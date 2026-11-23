"""문서 규칙 위반의 모델 수정: 공용 틀(app/worker/loop.py)의 작성과 출구 검사 (docs/DESIGN.md 4.7,
4.8, 5.3 (b)).

write는 모델 전용 폴더에서 고치는 호출(`ModelBackend.fix`)에 파이프라인 도구 둘(규칙 검사, 테스트)을
준다. 모델이 한 호출 안에서 테스트를 쓰고, 돌려 보고, 고치고, 다시 돌려 본다.
도구의 구현은 아래 verify가 쓰는 것과 같은 함수다(`check_now`, `judge_before`, `tools.run_suite`).
지시는 시스템 프롬프트로, 규칙 원문과 위반 목록은 사용자 메시지의 자료로 넘긴다. 회차가 적용한
diff와 마지막 답(설명 JSON)은 `model_cache`에 저장하고, 같은 입력이면 모델을 다시 부르지 않고
저장한 diff를 적용한다. 호출은 PR 상한(9.2)에서 센다.

verify는 올리기 직전의 출구 검사다. 모델이 도구 결과를 잘못 전하거나 PR 코드에 심어진 글에 속을 수
있어 같은 검사를 프로그램이 한 번 더 돌린다. 4.8의 순서대로 하고 앞에서 떨어지면 뒤는 하지 않는다:
허용 범위, 비밀값(여기까지 떨어지면 재시도 없이 버림), 설명 JSON, 규칙 재검사, 만든 테스트,
테스트, 크기 상한(여기는 이유를 다음 회차에 넘긴다). 조건에 맞지 않는 만든 테스트는 그 파일만 빼고
이어 간다. 샌드박스에서 무엇이든 돌리기 전에 앞의 둘을 끝낸다. 재검사가 읽는 base 설정은 worktree
밖에 있어 모델이 바꿀 수 없다 (4.8의 2번).
"""

import ast
import json
import logging
import os
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Final

from app.shared.db_pure import FixAttempt
from app.shared.job_pure import PRJob
from app.worker import mcp_pure, records_pure
from app.worker.ai.model_backend import FIX_TOOLS_TIMEOUT_S, AIEnvironmentError, EditCode
from app.worker.budget import CallBudget
from app.worker.call_budget_pure import Denied, PrScope
from app.worker.config.repo_config_pure import ModelSettings
from app.worker.core.bundle_pure import MODEL_BUNDLE
from app.worker.core.cache_key_pure import text_hash
from app.worker.core.diff_pure import ChangedLines, added_lines, change_blocks, new_files
from app.worker.core.violation_pure import Violation
from app.worker.doc_rules_pure import Found, outcomes_by_rule
from app.worker.fix import model_fix_pure, scope
from app.worker.fix.model_fix_pure import (
    Explanation,
    ModelFix,
    Picked,
    Target,
    Verification,
    Written,
)
from app.worker.fix.proposal_pure import (
    SECRET_BLOCKED,
    changed_line_count,
    find_secret,
    find_secret_in_text,
)
from app.worker.fix.scope_pure import (
    AI_POLICY,
    generated_test_dir,
    is_generated_test,
    is_pipeline_output,
    parse_porcelain,
)
from app.worker.git import repo
from app.worker.lang.python import read_sources
from app.worker.loop import (
    HEAVY_AFTER_FAILURES,
    LoopPolicy,
    LoopState,
    Verdict,
    WriteFailed,
    WriteResult,
    build_loop,
)
from app.worker.mcp_pure import ToolUsage
from app.worker.memory import FindsRecords
from app.worker.rag_pure import (
    EMPTY,
    Selection,
    merge_retrievals,
    query_text,
    render_records,
    retrieval,
)
from app.worker.rules.detect import Detection, source_lines
from app.worker.sandbox.safe_read import read_regular_file
from app.worker.state import Store
from app.worker.testing.generated_tests_pure import (
    Candidate,
    GeneratedTest,
    Suite,
    after_check,
    judge_tests,
    line_runs,
    render_before,
    render_check,
    render_now,
)
from app.worker.testing.test_lines import format_test_line
from app.worker.testing.test_run_pure import PytestRun

log = logging.getLogger(__name__)

MAX_FILE_BYTES: Final = 5 * 1024 * 1024
NOTHING_CHANGED: Final = "아무 파일도 바꾸지 않았다. 목록의 위반을 고친다."
# 수정 작성 호출 하나의 claude `--max-budget-usd` (9.2). 도구를 부르며 차례가 늘어 다른 단계(0.30,
# 0.50)보다 크게 둔다. 끝없이 도는 호출을 막을 뿐 상한 계산은 호출 수로 한다
FIX_CALL_BUDGET_USD: Final = {"light": 1.00, "heavy": 2.00}
_NEW_FILE_CODES: Final = frozenset({"??", " A"})
# 도구·검사기로 다시 확인하지 못하는 위반: 모델이 찾은 것, 받아들인 코멘트 (4.8의 5번, 4.11)
_UNCHECKED: Final = frozenset({"model", "feedback"})


def module_constants(worktree: Path, files: Sequence[str]) -> dict[str, list[str]]:
    """파일마다 이미 있는 모듈 수준 상수(대문자 이름). 호스트에서 파싱만 하고 실행하지 않는다."""
    result: dict[str, list[str]] = {}
    for file, source in read_sources(worktree, files).items():
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            continue
        names: list[str] = []
        for node in tree.body:
            targets = (
                node.targets if isinstance(node, ast.Assign) else [getattr(node, "target", None)]
            )
            for t in targets:
                if isinstance(t, ast.Name) and t.id.isupper() and t.id not in names:
                    names.append(t.id)
        result[file] = names
    return result


@dataclass
class FixTools:
    """수정 루프가 쓰는 외부 동작. 테스트에서는 가짜로 바꾼다."""

    # (worktree, 사용자 메시지, *, system_prompt, model, max_budget_usd, tools, timeout) → 결과.
    # 그래프는 모델 구현의 fix(`ModelBackend.fix`)를 넘긴다
    run_model: EditCode
    # 도구·검사기로 확인하는 모든 규칙을 다시 돌린다: (바뀐 줄, 새 파일) → 위반
    recheck: Callable[[ChangedLines, frozenset[str]], Detection]
    rules_rechecked: int  # recheck가 돌리는 도구·검사기 수 (수정안 PR의 검증 절에 쓴다)
    # 샌드박스 테스트 (줄마다 실행한 테스트까지): (만든 테스트의 내용, 줄을 볼 소스 파일) → 결과.
    # 내용이 None이면 worktree의 지금 상태, 있으면 PR head에 그 파일만 얹은 따로 된 worktree
    run_suite: Callable[[Mapping[str, bytes] | None, Sequence[str]], Suite]
    is_stale: Callable[[], bool] = lambda: False
    secrets: Sequence[str] = ()  # 파이프라인의 비밀값
    # 모델 구현이 수정 작성을 할 수 없는 까닭(`model_backend.fix_unavailable`). 있으면 모델을 부르지
    # 않고 그 까닭으로 끝낸다
    unavailable: str = ""


class _Cancelled(Exception):
    pass


class _CapHit(Exception):
    pass


@dataclass(frozen=True)
class _Passed:
    """출구 검사를 통과한 회차의 결과."""

    explanation: Explanation
    files: tuple[str, ...]
    verification: Verification
    tests: tuple[GeneratedTest, ...]
    untested: Mapping[tuple[str, str], str]


@dataclass
class _Tally:
    calls: int = 0
    cache_hits: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    models: list[str] = field(default_factory=list)


def file_hashes(worktree: Path, files: Sequence[str]) -> dict[str, str]:
    out = {}
    for rel in files:
        path = repo.worktree_file(worktree, rel)
        data = read_regular_file(path, max_bytes=MAX_FILE_BYTES) if path is not None else None
        out[rel] = text_hash(data.decode("utf-8", errors="replace")) if data is not None else ""
    return out


def notes_for(found: Found, picked: Picked, worktree: Path) -> tuple[records_pure.Target, ...]:
    """고치기 전에 대상 위반마다 규칙 원문과 위반 줄 앞뒤를 남긴다. 그 수정안이 머지·닫히거나
    검증에 실패하면 이것으로 기록을 만든다 (4.10)."""
    texts = {rule_id: o.rule.text for rule_id, o in outcomes_by_rule(found).items()}
    lines: dict[str, list[str]] = {}
    for v in picked.targets:
        if v.file not in lines:
            path = repo.worktree_file(worktree, v.file)
            data = read_regular_file(path, max_bytes=MAX_FILE_BYTES) if path is not None else None
            lines[v.file] = data.decode("utf-8", errors="replace").split("\n") if data else []
    return tuple(
        records_pure.Target(
            v.id, v.rule_id, texts.get(v.rule_id, ""), v.file, v.line, v.line_text or "",
            records_pure.context_around(lines[v.file], v.line),
        )
        for v in picked.targets
    )  # fmt: skip


def save_attempt(
    store: Store,
    job: PRJob,
    merge_base: str,
    fix: ModelFix,
    notes: Sequence[records_pure.Target],
    worktree: Path,
    bundle: str = MODEL_BUNDLE,
) -> ModelFix:
    """시도 결과를 `fix_attempts`에 저장하고 그 id를 붙인다. 검증을 통과했으면 지금 worktree의
    패치와 바뀐 파일의 해시도 남긴다. 전달은 이 패치만 올린다 (4.8 끝). bundle은 코멘트 수정이면
    `feedback`이다 (4.11)."""
    ready = fix.ready
    explanation = asdict(fix.explanation) if fix.explanation is not None else None
    attempt_id = store.save_fix_attempt(
        FixAttempt(
            repo=job.repo, pr_number=job.pr_number, head_sha=job.head_sha, merge_base=merge_base,
            bundle=bundle, status=fix.status, attempts=fix.attempts,
            models_json=json.dumps(list(fix.models)), detail=fix.detail,
            targets_json=records_pure.targets_to_json(notes),
            patch=repo.head_diff(worktree) if ready else None,
            explanation_json=json.dumps(explanation, ensure_ascii=False) if ready else None,
            file_hashes_json=(
                json.dumps(file_hashes(worktree, fix.changed_files)) if ready else None
            ),
            references_json=json.dumps(sorted(
                {ref.record_id for r in fix.retrievals for ref in r.references}
            )) if fix.retrievals else None,
        )
    )  # fmt: skip
    return replace(fix, attempt_id=attempt_id)


def reapply(store: Store, attempt_id: int | None, worktree: Path) -> FixAttempt | str:
    """검증을 통과한 시도의 패치를 새 worktree에 적용하고 바뀐 파일의 해시가 저장한 값과 같은지
    본다. 모델을 부르지 않는다. 같으면 그 시도, 다르거나 적용하지 못하면 올리지 않는 이유."""
    attempt = store.fix_attempt(attempt_id) if attempt_id is not None else None
    if attempt is None or attempt.patch is None or attempt.file_hashes_json is None:
        return "검증을 통과한 수정안을 저장한 기록이 없음"
    try:
        repo.apply_patch(worktree, attempt.patch.encode())
    except repo.GitError as e:
        log.warning("저장한 수정안을 다시 적용하지 못함 (시도 %s): %s", attempt_id, e)
        return "저장한 수정안을 새 worktree에 다시 적용하지 못함"
    expected = json.loads(attempt.file_hashes_json)
    if file_hashes(worktree, sorted(expected)) != expected:
        log.warning("다시 적용한 수정안의 파일 해시가 다름 (시도 %s)", attempt_id)
        return "다시 적용한 수정안이 검증한 파일과 다름"
    return attempt


def _top_dirs(worktree: Path) -> list[str]:
    """worktree 맨 위의 진짜 폴더 이름 (링크는 뺀다)."""
    try:
        entries = list(os.scandir(worktree))
    except OSError:
        return []
    return [e.name for e in entries if stat.S_ISDIR(e.stat(follow_symlinks=False).st_mode)]


def _read_generated(worktree: Path, paths: Sequence[str]) -> dict[str, bytes]:
    out = {}
    for rel in paths:
        path = repo.worktree_file(worktree, rel)
        data = read_regular_file(path, max_bytes=MAX_FILE_BYTES) if path is not None else None
        if data is not None:
            out[rel] = data
    return out


def fix_violations(
    found: Found,
    picked: Picked,
    *,
    worktree: Path,
    merge_base: str,
    test_before: PytestRun,
    store: Store,
    budget: CallBudget,
    models: ModelSettings,
    max_attempts: int,
    tools: FixTools,
    generation: int = model_fix_pure.GENERATION,
    retriever: FindsRecords | None = None,
    context_of: Callable[[Violation], str] = lambda v: "",
    note: str = "",
) -> ModelFix | None:
    """picked.targets를 모델로 고치고 출구 검사를 한다. 새 커밋이 와서 중단되면 None.

    found는 수정 전 위반 전체, picked는 `model_fix.pick_targets`의 결과다(대상이 하나 이상).
    generation은 시도 세대다. `/fix`가 다시 돌릴 때마다 올라 저장된 수정 작성을 쓰지 않는다 (4.9).
    retriever가 있으면(검색을 켬) 첫 회차에는 비슷한 채택 기록을, 재시도에는 비슷한 실패 기록을
    참고 자료로 넣는다 (6.2). context_of(위반)는 위반 줄과 앞뒤 5줄이다. note는 시스템 프롬프트
    뒤에 붙이는 글이다(코멘트 수정의 안내, 4.11). 저장 키에도 들어간다.
    """
    by_rule = {rule_id: o.rule for rule_id, o in outcomes_by_rule(found).items()}
    targets = picked.targets
    allowed = sorted({v.file for v in targets})
    tally = _Tally()
    passed: list[_Passed] = []  # 통과한 회차의 결과
    found_for: dict[str, Selection] = {}  # 쓰는 곳(fix, fix_retry) → 고른 기록. 한 번만 찾는다
    usage: list[ToolUsage] = []
    kept: list[str] = []  # 앞 회차의 출구 검사가 받아들인 만든 테스트 (회차를 넘어 겹치지 않음)

    def records_for(site: str) -> Selection:
        if retriever is None:
            return EMPTY
        if site not in found_for:
            queries = [query_text(by_rule[v.rule_id].text, context_of(v)) for v in targets]
            found_for[site] = retriever.find(site, list(dict.fromkeys(queries)))
        return found_for[site]

    def outcome(status: model_fix_pure.ModelFixStatus, attempts: int, detail: str = "") -> ModelFix:
        last = passed[-1] if passed else None
        found = merge_retrievals(retrieval(s, site) for site, s in found_for.items())
        error = retriever.error if retriever is not None else ""
        return ModelFix(
            status=status, targets=tuple(v.id for v in targets),
            size_capped=tuple(v.id for v in picked.size_capped), detail=detail,
            explanation=last.explanation if last else None,
            verification=last.verification if last else None,
            changed_files=last.files if last else (),
            calls=tally.calls, cache_hits=tally.cache_hits, input_tokens=tally.input_tokens,
            output_tokens=tally.output_tokens, models=tuple(tally.models), attempts=attempts,
            retrievals=tuple(replace(r, error=error) for r in found),
            generated=last.tests if last else (),
            line_runs=line_runs(targets, last.tests, last.untested) if last else (),
            tool_usage=sum(usage, ToolUsage()),
        )  # fmt: skip

    if tools.unavailable:
        return outcome("refused", 0, f"수정안을 만들지 않음: {tools.unavailable}")
    outside = repo.outside_symlinks(worktree)
    if outside:  # 링크 폴더를 거친 쓰기는 worktree 밖으로 가고 범위 검사에도 안 보인다
        shown = ", ".join(outside[:5])
        return outcome("refused", 0, f"worktree 밖을 가리키는 심볼릭 링크: {shown}")
    # 모델을 처음 실행하기 직전에 한 번만 찍는다. 재시도 사이에 다시 찍지 않는다 (4.8의 1번)
    ignored_before = scope.snapshot_ignored(worktree)
    test_dir = generated_test_dir(_top_dirs(worktree))
    policy = replace(AI_POLICY, generated_test_dir=test_dir)
    constants = module_constants(worktree, allowed)
    start = file_hashes(worktree, allowed)
    sources_before = read_sources(worktree, allowed)  # 만든 테스트를 고치기 전 코드로 판정한다
    # 모델이 찾은 위반은 도구로 다시 확인하지 못해 그 줄이 바뀌었는지만 본다 (4.8의 5번).
    # 받아들인 코멘트(feedback)는 검사기가 없어 그 파일이 바뀌었는지만 본다 (4.11)
    by_model = [v for v in targets if v.found_by == "model"]
    by_feedback = [v for v in targets if v.found_by == "feedback"]
    by_tools = [v for v in targets if v.found_by not in _UNCHECKED]
    lines_before = {f: source_lines(worktree, f) for f in {v.file for v in by_model}}
    asked = [Target(v, by_rule[v.rule_id]) for v in targets]

    def generated_now() -> list[str]:
        """정한 자리에 새로 만든 테스트 파일. diff에 보이게 `git add -N`한다."""
        paths = sorted(
            e.path for e in parse_porcelain(repo.status(worktree))
            if e.orig_path is None and e.code in _NEW_FILE_CODES
            and is_generated_test(e.path, test_dir)
        )  # fmt: skip
        repo.track_new(worktree, paths)
        return paths

    def check_now() -> tuple[list[Violation], list[Violation], list[str]]:
        """규칙 재검사 (4.8의 5번): (남은 대상 위반, 새 위반, 검사하지 못한 규칙)."""
        generated_now()
        diff = repo.working_diff(worktree, merge_base)
        detection = tools.recheck(added_lines(diff), new_files(diff))
        unchecked = [f"{u.rule.id}: {u.detail}" for u in detection.unchecked]
        before = [v for v in found.violations if v.found_by not in _UNCHECKED]
        left, new = model_fix_pure.recheck(before, by_tools, detection.violations)
        lines_after = {f: source_lines(worktree, f) for f in lines_before}
        left += model_fix_pure.unchanged_lines(by_model, lines_before, lines_after)
        now = file_hashes(worktree, sorted({v.file for v in by_feedback}))
        left += [v for v in by_feedback if now[v.file] == start[v.file]]
        return left, new, unchecked

    def judge_before() -> tuple[tuple[GeneratedTest, ...], Suite]:
        """만든 테스트를 고치기 전 코드에 얹어 돌리고 받아들일지 정한다 (4.8의 6번)."""
        contents = _read_generated(worktree, generated_now())
        suite = tools.run_suite(contents, allowed)
        candidates = [Candidate(f, d.decode("utf-8", errors="replace"))
                      for f, d in contents.items()]  # fmt: skip
        return judge_tests(candidates, targets, suite, sources_before, kept=kept), suite

    def check_tool(args: Mapping[str, str]) -> str:
        left, new, unchecked = check_now()
        return render_check(left, new, unchecked, len(by_model))

    def test_tool(args: Mapping[str, str]) -> str:
        if args.get("code") == "before":
            judged, suite = judge_before()
            return render_before(judged, suite, targets)
        return render_now(tools.run_suite(None, allowed), generated_now())

    pipeline_tools = {mcp_pure.CHECK_RULES: check_tool, mcp_pure.RUN_TESTS: test_tool}

    def write(state: LoopState) -> WriteResult:
        attempt, tier = state["attempt"], state["tier"]
        model = models.heavy if tier == "heavy" else models.light
        feedback = state.get("feedback", [])
        chosen = records_for("fix" if attempt == 1 else "fix_retry")
        records = render_records(chosen.chosen)
        key = model_fix_pure.write_key(
            targets, start, file_hashes(worktree, allowed), feedback, model, generation,
            records=chosen.ids, note=note,
        )  # fmt: skip
        entry = store.cached_result(key)
        if entry is not None:
            written = model_fix_pure.written_from_json(entry.result_json)
            if written.patch:
                try:
                    repo.apply_patch(worktree, written.patch)
                except repo.GitError as e:
                    raise WriteFailed(f"저장한 수정을 적용하지 못함: {e}") from e
            budget.record_hit(PrScope(), stage="fix", tier=tier, model=model)
            tally.cache_hits += 1
            return WriteResult(artifact=key)
        call = budget.begin(PrScope(), stage="fix", tier=tier, model=model, attempt=attempt)
        if isinstance(call, Denied):
            raise _CapHit(call.reason)
        prompt = model_fix_pure.build_prompt(
            asked, constants=constants, feedback=feedback,
            current_diff=repo.head_diff(worktree) if attempt > 1 else "", records=records,
            test_dir=test_dir,
        )  # fmt: skip
        system_prompt = (
            model_fix_pure.SYSTEM_PROMPT + (model_fix_pure.RECORDS_NOTE if records else "") + note
        )
        result = tools.run_model(
            worktree, prompt, system_prompt=system_prompt, model=model,
            max_budget_usd=FIX_CALL_BUDGET_USD[tier], tools=pipeline_tools,
            timeout=FIX_TOOLS_TIMEOUT_S,
        )  # fmt: skip
        failure = None if result.ok else f"모델 호출 실패: {result.error}: {result.detail}"
        budget.finish(
            call, ok=result.ok, error=failure, model_id=result.model_id,
            input_tokens=result.input_tokens, output_tokens=result.output_tokens,
            permission_denials=len(result.permission_denials),
        )  # fmt: skip
        tally.calls += 3 if tier == "heavy" else 1
        tally.input_tokens += result.input_tokens or 0
        tally.output_tokens += result.output_tokens or 0
        tally.models.append(result.model_id or model)
        usage.append(result.tool_usage)
        if result.tool_usage.calls:
            log.info("수정 모델의 도구 호출: 규칙 검사 %d회, 테스트 %d회, %.0f초%s",
                     result.tool_usage.check_rules, result.tool_usage.run_tests,
                     result.tool_usage.seconds,
                     " (상한에 닿음)" if result.tool_usage.limited else "")  # fmt: skip
        if result.permission_denials:
            log.warning("수정 모델이 허용 범위 밖 도구 호출을 시도함 (%d건): %s",
                        len(result.permission_denials),
                        ", ".join(d.tool_name for d in result.permission_denials))  # fmt: skip
        if result.unexpected_tools:
            log.warning("수정 모델이 목록에 없는 도구를 부르려 함: %s",
                        ", ".join(f"{n} {c}회" for n, c in result.unexpected_tools))  # fmt: skip
        if result.environment:
            raise AIEnvironmentError(result.environment)
        if tools.is_stale():
            raise _Cancelled
        if failure is not None:
            raise WriteFailed(failure)
        store.save_result(
            key, stage="fix", model=result.model_id or model,
            prompt_version=model_fix_pure.PROMPT_VERSION,
            result_json=model_fix_pure.written_to_json(Written(result.applied, result.text)),
        )  # fmt: skip
        return WriteResult(artifact=key)

    def verify(state: LoopState) -> Verdict:
        entry = store.cached_result(state["artifact"])
        if entry is None:
            raise LookupError(f"저장한 수정이 없다: {state['artifact']}")
        written = model_fix_pure.written_from_json(entry.result_json)
        checked = scope.check_scope(worktree, set(allowed), ignored_before, policy=policy)
        if not checked.ok:
            return Verdict("drop", f"허용 범위 밖의 변경: {checked.detail}")
        entries = [
            e for e in parse_porcelain(repo.status(worktree)) if not is_pipeline_output(e.path)
        ]
        if not entries:
            return Verdict("retry", "아무 파일도 바꾸지 않음", (NOTHING_CHANGED,))
        generated = generated_now()
        patch = repo.head_diff(worktree)
        kind = find_secret(patch, tools.secrets) or find_secret_in_text(written.text, tools.secrets)
        if kind is not None:
            return Verdict("drop", f"{SECRET_BLOCKED} ({kind})")

        try:
            answer = model_fix_pure.parse_answer(written.text)
        except ValueError as e:
            return Verdict("retry", f"설명 JSON을 읽지 못함: {e}")
        code_blocks = [b for b in change_blocks(patch) if b.file not in generated]
        explanation = model_fix_pure.explain(answer, code_blocks, targets)
        if isinstance(explanation, model_fix_pure.Mismatch):
            detail = f"설명이 수정과 맞지 않음: {explanation.detail}"
            return Verdict("retry", detail, (detail, *explanation.feedback))

        left, new, unchecked = check_now()
        if unchecked:
            return Verdict("retry", f"고친 뒤 규칙 재검사를 하지 못함: {'; '.join(unchecked)}")
        if left or new:
            feedback = model_fix_pure.violation_feedback(
                "아직 남은 위반", left
            ) + model_fix_pure.violation_feedback("새로 생긴 위반", new)
            detail = f"남은 위반 {len(left)}건, 새 위반 {len(new)}건"
            return Verdict("retry", detail, tuple(feedback))

        judged: tuple[GeneratedTest, ...] = ()
        if generated:
            judged, before_run = judge_before()
            if before_run.run.cancelled:
                raise _Cancelled
            for t in judged:
                if not t.accepted:  # 조건에 맞지 않는 테스트는 그 파일만 뺀다
                    (worktree / t.file).unlink(missing_ok=True)
                    repo.untrack(worktree, t.file)
        after = tools.run_suite(None, allowed)
        if after.run.cancelled:
            raise _Cancelled
        check = after_check(judged, test_before, after)
        if check.broken:
            failed = [n for n in (after.run.summary.failed_tests if after.run.summary else ())]
            detail = f"만든 테스트 {len(check.broken)}개가 고치기 전에는 통과했는데 고친 뒤 실패함"
            feedback = [f"만든 테스트가 고친 뒤 실패함 (수정이 동작을 바꿈): {f}"
                        for f in check.broken]  # fmt: skip
            return Verdict("retry", detail, (*feedback, *(f"실패한 테스트: {n}" for n in failed)))
        if not check.existing_ok:
            line = format_test_line(after.run)
            failed = list(after.run.summary.failed_tests) if after.run.summary else []
            return Verdict(
                "retry",
                f"고친 뒤 테스트: {line}",
                (f"고친 뒤 테스트: {line}", *(f"실패한 테스트: {t}" for t in failed)),
            )
        kept[:] = [t.file for t in check.tests if t.accepted]

        entries = [
            e for e in parse_porcelain(repo.status(worktree)) if not is_pipeline_output(e.path)
        ]
        files = sorted({e.path for e in entries})
        test_files = set(kept)
        code_files = [f for f in files if f not in test_files]
        test_lines = sum(t.lines for t in check.tests if t.accepted)
        lines = changed_line_count(repo.head_diff(worktree)) - test_lines
        too_big = model_fix_pure.size_problem(len(code_files), lines)
        if too_big is not None:
            return Verdict("retry", too_big)
        verification = Verification(
            tests=test_before.passed_count, resolved=len(targets),
            rules_rechecked=tools.rules_rechecked, files=len(code_files), lines=lines,
            unverified=len(by_model) + len(by_feedback),
        )  # fmt: skip
        untested = {(n.file, n.rule): n.why for n in explanation.untested}
        passed.append(_Passed(explanation, tuple(files), verification, check.tests, untested))
        return Verdict("pass")

    # 두 단계가 같은 모델이면(파이프라인 쪽 `model.default` 하나로 정한 때) 올려도 모델이 같아
    # 호출 수만 셋으로 센다. 올리지 않고 가벼운 단계로 회차를 채운다
    heavy_after = None if models.heavy == models.light else HEAVY_AFTER_FAILURES
    policy_ = LoopPolicy(
        max_attempts=max_attempts, may_call=budget.may_call(PrScope()), heavy_after=heavy_after
    )
    try:
        final = build_loop("fix_model", write, verify, policy_).invoke({"task": {}})
    except _Cancelled:
        return None
    except _CapHit as e:
        return outcome("budget", len(tally.models), str(e))
    attempts = final.get("attempt", 0)
    match final["outcome"]:
        case "accepted":
            return outcome("ready", attempts)
        case "dropped":
            return outcome("dropped", attempts, final.get("detail", ""))
        case "budget":
            denied = budget.decide(PrScope(), "light")
            reason = denied.reason if isinstance(denied, Denied) else "호출 상한"
            return outcome("budget", attempts, reason)
        case _:
            return outcome("failed", attempts, final.get("detail", ""))
