"""문서 규칙 위반의 모델 수정: 공용 틀(worker/loop.py)의 작성과 결정적 검증 (docs/DESIGN.md 4.7,
4.8, 5.3 (b)).

write는 모델 전용 폴더(`workdir.run_in_model_folder`)에서 파일 도구만 준 모델을 부른다. 지시는
시스템 프롬프트로, 규칙 원문과 위반 목록은 사용자 메시지의 자료로 넘긴다. 회차가 적용한 diff와
마지막 답(설명 JSON)은 `model_cache`에 저장하고, 같은 입력이면 모델을 다시 부르지 않고 저장한
diff를 적용한다. 호출은 PR 상한(9.2)에서 센다.

verify는 모델을 쓰지 않는다. 4.8의 순서대로 하고 앞에서 떨어지면 뒤는 하지 않는다: 허용 범위,
파이프라인 파일, 비밀값(여기까지 떨어지면 재시도 없이 버림), 설명 JSON, 규칙 재검사, 테스트,
크기 상한(여기는 이유를 다음 회차에 넘긴다). 샌드박스에서 무엇이든 돌리기 전에 앞의 셋을 끝낸다.
"""

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Final

from app.models import model_fix as rules
from app.models import records as record_rules
from app.models.ai_fix import CALL_BUDGET_USD
from app.models.cache_key import text_hash
from app.models.call_budget import Denied, PrScope
from app.models.db import FixAttempt
from app.models.diff import ChangedLines, added_lines, change_blocks, new_files
from app.models.doc_rules import Found
from app.models.finding import Finding, SkillResult
from app.models.fix import code_fix_tests_ok, compare
from app.models.job import PRJob
from app.models.model_fix import (
    Explanation,
    ModelFix,
    Picked,
    Target,
    Verification,
    Written,
)
from app.models.proposal import changed_line_count, find_secret, find_secret_in_text
from app.models.repo_config import ModelSettings
from app.models.scope import is_pipeline_output, parse_porcelain
from app.models.test_run import PytestRun
from app.sandbox.safe_read import read_regular_file
from app.worker import repo, scope
from app.worker.ai.claude_cli import ClaudeResult
from app.worker.ai_fix import AIEnvironmentError, environment_problem, module_constants
from app.worker.budget import CallBudget
from app.worker.detect import Detection
from app.worker.loop import LoopPolicy, LoopState, Verdict, WriteFailed, WriteResult, build_loop
from app.worker.proposal import MODEL_BUNDLE
from app.worker.report import format_test_line
from app.worker.state import Store

log = logging.getLogger(__name__)

MAX_FILE_BYTES: Final = 5 * 1024 * 1024
MAX_GUARDED_BYTES: Final = 1024 * 1024
NOTHING_CHANGED: Final = "아무 파일도 바꾸지 않았다. 목록의 위반을 고친다."


@dataclass
class FixTools:
    """수정 루프가 쓰는 외부 동작. 테스트에서는 가짜로 바꾼다."""

    # (worktree, 사용자 메시지, *, system_prompt, model, max_budget_usd) → 결과.
    # 그래프는 workdir.run_in_model_folder를 넘긴다
    run_model: Callable[..., ClaudeResult]
    # 문서 규칙의 검사기를 다시 돌린다: (바뀐 줄, 새 파일) → 위반
    recheck: Callable[[ChangedLines, frozenset[str]], Detection]
    rules_rechecked: int  # recheck가 돌리는 검사기 수 (수정안 PR의 검증 절에 쓴다)
    run_tests: Callable[[], PytestRun]
    # v1 lint의 재검사. lint가 꺼져 있거나 수정 전 결과가 없으면 None. 중단되면 None을 낸다
    recheck_lint: Callable[[ChangedLines], SkillResult | None] | None = None
    is_stale: Callable[[], bool] = lambda: False
    # 재검사가 읽는 base 설정 사본(`.pipeline-out/base/`). 범위 검사에서 빠지므로 내용을 따로 본다
    guarded: tuple[str, ...] = ()
    secrets: Sequence[str] = ()  # 파이프라인의 비밀값


class _Cancelled(Exception):
    pass


class _CapHit(Exception):
    pass


@dataclass
class _Tally:
    calls: int = 0
    cache_hits: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    models: list[str] = field(default_factory=list)


def _file_hashes(worktree: Path, files: Sequence[str]) -> dict[str, str]:
    out = {}
    for rel in files:
        path = repo.worktree_file(worktree, rel)
        data = read_regular_file(path, max_bytes=MAX_FILE_BYTES) if path is not None else None
        out[rel] = text_hash(data.decode("utf-8", errors="replace")) if data is not None else ""
    return out


def notes_for(found: Found, picked: Picked, worktree: Path) -> tuple[record_rules.Target, ...]:
    """고치기 전에 대상 위반마다 규칙 원문과 위반 줄 앞뒤를 남긴다. 그 수정안이 머지·닫히거나
    검증에 실패하면 이것으로 기록을 만든다 (4.10)."""
    texts = {o.rule.id: o.rule.text for o in found.outcomes}
    lines: dict[str, list[str]] = {}
    for v in picked.targets:
        if v.file not in lines:
            path = repo.worktree_file(worktree, v.file)
            data = read_regular_file(path, max_bytes=MAX_FILE_BYTES) if path is not None else None
            lines[v.file] = data.decode("utf-8", errors="replace").split("\n") if data else []
    return tuple(
        record_rules.Target(
            v.id, v.rule_id, texts.get(v.rule_id, ""), v.file, v.line, v.line_text or "",
            record_rules.context_around(lines[v.file], v.line),
        )
        for v in picked.targets
    )  # fmt: skip


def save_attempt(
    store: Store,
    job: PRJob,
    merge_base: str,
    fix: ModelFix,
    notes: Sequence[record_rules.Target],
    worktree: Path,
) -> ModelFix:
    """시도 결과를 `fix_attempts`에 저장하고 그 id를 붙인다. 검증을 통과했으면 지금 worktree의
    패치와 바뀐 파일의 해시도 남긴다. 전달은 이 패치만 올린다 (4.8 끝)."""
    ready = fix.ready
    explanation = asdict(fix.explanation) if fix.explanation is not None else None
    attempt_id = store.save_fix_attempt(
        FixAttempt(
            repo=job.repo, pr_number=job.pr_number, head_sha=job.head_sha, merge_base=merge_base,
            bundle=MODEL_BUNDLE, status=fix.status, attempts=fix.attempts,
            models_json=json.dumps(list(fix.models)), detail=fix.detail,
            targets_json=record_rules.targets_to_json(notes),
            patch=repo.head_diff(worktree) if ready else None,
            explanation_json=json.dumps(explanation, ensure_ascii=False) if ready else None,
            file_hashes_json=(
                json.dumps(_file_hashes(worktree, fix.changed_files)) if ready else None
            ),
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
    if _file_hashes(worktree, sorted(expected)) != expected:
        log.warning("다시 적용한 수정안의 파일 해시가 다름 (시도 %s)", attempt_id)
        return "다시 적용한 수정안이 검증한 파일과 다름"
    return attempt


def fix_violations(
    found: Found,
    picked: Picked,
    *,
    worktree: Path,
    merge_base: str,
    test_before: PytestRun,
    lint_before: SkillResult | None,
    store: Store,
    budget: CallBudget,
    models: ModelSettings,
    max_attempts: int,
    tools: FixTools,
    generation: int = rules.GENERATION,
) -> ModelFix | None:
    """picked.targets를 모델로 고치고 검증한다. 새 커밋이 와서 중단되면 None.

    found는 수정 전 위반 전체, picked는 `model_fix.pick_targets`의 결과다(대상이 하나 이상).
    generation은 시도 세대다. `/fix`가 다시 돌릴 때마다 올라 저장된 수정 작성을 쓰지 않는다 (4.9).
    """
    by_rule = {o.rule.id: o.rule for o in found.outcomes}
    targets = picked.targets
    allowed = sorted({v.file for v in targets})
    target_rules = frozenset(v.rule_id for v in targets)
    tally = _Tally()
    passed: list[tuple[Explanation, tuple[str, ...], Verification]] = []  # 통과한 회차의 결과

    def outcome(status: rules.ModelFixStatus, attempts: int, detail: str = "") -> ModelFix:
        explanation, files, verification = passed[-1] if passed else (None, (), None)
        return ModelFix(
            status=status, targets=tuple(v.id for v in targets),
            size_capped=tuple(v.id for v in picked.size_capped), detail=detail,
            explanation=explanation, verification=verification, changed_files=files,
            calls=tally.calls, cache_hits=tally.cache_hits, input_tokens=tally.input_tokens,
            output_tokens=tally.output_tokens, models=tuple(tally.models), attempts=attempts,
        )  # fmt: skip

    outside = repo.outside_symlinks(worktree)
    if outside:  # 링크 폴더를 거친 쓰기는 worktree 밖으로 가고 범위 검사에도 안 보인다
        shown = ", ".join(outside[:5])
        return outcome("refused", 0, f"worktree 밖을 가리키는 심볼릭 링크: {shown}")
    guarded = {rel: read_regular_file(worktree / rel, max_bytes=MAX_GUARDED_BYTES)
               for rel in tools.guarded}  # fmt: skip
    # 모델을 처음 실행하기 직전에 한 번만 찍는다. 재시도 사이에 다시 찍지 않는다 (4.8의 1번)
    ignored_before = scope.snapshot_ignored(worktree)
    constants = module_constants(worktree, allowed)
    start = _file_hashes(worktree, allowed)
    asked = [Target(v, by_rule[v.rule_id]) for v in targets]

    def write(state: LoopState) -> WriteResult:
        attempt, tier = state["attempt"], state["tier"]
        model = models.heavy if tier == "heavy" else models.light
        feedback = state.get("feedback", [])
        key = rules.write_key(
            targets, start, _file_hashes(worktree, allowed), feedback, model, generation
        )
        entry = store.cached_result(key)
        if entry is not None:
            written = rules.written_from_json(entry.result_json)
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
        prompt = rules.build_prompt(
            asked, constants=constants, feedback=feedback,
            current_diff=repo.head_diff(worktree) if attempt > 1 else "",
        )  # fmt: skip
        result = tools.run_model(
            worktree, prompt, system_prompt=rules.SYSTEM_PROMPT, model=model,
            max_budget_usd=CALL_BUDGET_USD[tier],
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
        if result.permission_denials:
            log.warning("수정 모델이 허용 범위 밖 도구 호출을 시도함 (%d건): %s",
                        len(result.permission_denials),
                        ", ".join(d.tool_name for d in result.permission_denials))  # fmt: skip
        if result.unexpected_tools:
            log.warning("수정 모델이 목록에 없는 도구를 부르려 함: %s",
                        ", ".join(f"{n} {c}회" for n, c in result.unexpected_tools))  # fmt: skip
        problem = environment_problem(result)
        if problem is not None:
            raise AIEnvironmentError(problem)
        if tools.is_stale():
            raise _Cancelled
        if failure is not None:
            raise WriteFailed(failure)
        store.save_result(
            key, stage="fix", model=result.model_id or model,
            prompt_version=rules.PROMPT_VERSION,
            result_json=rules.written_to_json(Written(result.applied, result.text)),
        )  # fmt: skip
        return WriteResult(artifact=key)

    def verify(state: LoopState) -> Verdict:
        entry = store.cached_result(state["artifact"])
        if entry is None:
            raise LookupError(f"저장한 수정이 없다: {state['artifact']}")
        written = rules.written_from_json(entry.result_json)
        checked = scope.check_scope(worktree, set(allowed), ignored_before)
        if not checked.ok:
            return Verdict("drop", f"허용 범위 밖의 변경: {checked.detail}")
        tampered = [
            rel
            for rel, data in guarded.items()
            if read_regular_file(worktree / rel, max_bytes=MAX_GUARDED_BYTES) != data
        ]
        if tampered:  # fmt: skip
            return Verdict("drop", f"파이프라인 설정 파일이 바뀜: {', '.join(tampered)}")
        entries = [
            e for e in parse_porcelain(repo.status(worktree)) if not is_pipeline_output(e.path)
        ]
        if not entries:
            return Verdict("retry", "아무 파일도 바꾸지 않음", (NOTHING_CHANGED,))
        patch = repo.head_diff(worktree)
        kind = find_secret(patch, tools.secrets) or find_secret_in_text(written.text, tools.secrets)
        if kind is not None:
            return Verdict("drop", f"비밀값으로 보이는 내용이 있음 ({kind})")

        try:
            explanation = rules.parse_explanation(written.text)
        except ValueError as e:
            return Verdict("retry", f"설명 JSON을 읽지 못함: {e}")
        problems = rules.explanation_problems(explanation, change_blocks(patch), target_rules)
        if problems:
            return Verdict("retry", f"설명이 수정과 맞지 않음: {problems[0]}", tuple(problems))

        diff = repo.working_diff(worktree, merge_base)
        changed = added_lines(diff)
        detection = tools.recheck(changed, new_files(diff))
        if detection.unchecked:
            what = "; ".join(f"{u.rule.id}: {u.detail}" for u in detection.unchecked)
            return Verdict("retry", f"고친 뒤 규칙 재검사를 하지 못함: {what}")
        left, new = rules.recheck(found.violations, targets, detection.violations)
        new_lint: list[Finding] = []
        if tools.recheck_lint is not None and lint_before is not None and lint_before.checked:
            lint_after = tools.recheck_lint(changed)
            if lint_after is None:
                raise _Cancelled
            if lint_after.checked:
                new_lint = compare(lint_before.findings, lint_after)[1]
        if left or new or new_lint:
            feedback = (
                rules.violation_feedback("아직 남은 위반", left)
                + rules.violation_feedback("새로 생긴 위반", new)
                + [f"새로 생긴 lint 위반: {f.file}:{f.line} {f.code} {f.message}" for f in new_lint]
            )
            detail = f"남은 위반 {len(left)}건, 새 위반 {len(new) + len(new_lint)}건"
            return Verdict("retry", detail, tuple(feedback))

        after = tools.run_tests()
        if after.cancelled:
            raise _Cancelled
        if not code_fix_tests_ok(test_before, after):
            line = format_test_line(after)
            failed = list(after.summary.failed_tests) if after.summary else []
            return Verdict(
                "retry",
                f"고친 뒤 테스트: {line}",
                (f"고친 뒤 테스트: {line}", *(f"실패한 테스트: {t}" for t in failed)),
            )

        files = sorted({e.path for e in entries})
        lines = changed_line_count(patch)
        too_big = rules.size_problem(len(files), lines)
        if too_big is not None:
            return Verdict("retry", too_big)
        verification = Verification(
            tests=after.passed_count, resolved=len(targets),
            rules_rechecked=tools.rules_rechecked,
            lint_rechecked=tools.recheck_lint is not None and lint_before is not None,
            files=len(files), lines=lines,
        )  # fmt: skip
        passed.append((explanation, tuple(files), verification))
        return Verdict("pass")

    policy = LoopPolicy(max_attempts=max_attempts, may_call=budget.may_call(PrScope()))
    try:
        final = build_loop("fix_model", write, verify, policy).invoke({"task": {}})
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
