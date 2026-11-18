"""AI 수정 루프: 작성 → 결정적 검증 → 재시도 (docs/DESIGN.md F7).

static과 structure가 같이 쓴다. 스킬마다 다른 것은 프롬프트(`models/ai_fix.SKILL_GUIDE`)와 다시
검사하는 함수(`CodeFixTools.recheck`)뿐이다. AI는 수정안만 쓰고, 통과 여부는 범위 검사·규칙
재검사·샌드박스 테스트가 정한다.

한 회차의 순서 (보안 순서가 중요하다):
1. AI 실행 (worktree를 cwd로, 파일 도구만)
2. 허용 범위 검사: 바뀐 파일과 새로 생긴 무시된 파일. 샌드박스에서 아무것도 실행하기 전에 본다.
   범위 밖이면 재시도하지 않고 수정안 전체를 버린다
3. 규칙 재검사 (그 스킬 + lint): 원래 위반 해소, 새 위반 없음
4. 샌드박스 테스트: 수정 전과 같은 수가 모두 통과
실패하면 이유와 남은 위반을 다음 회차의 프롬프트에 넣는다. worktree는 되돌리지 않고 이어서 고친다.
"""

import ast
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from app.models import ai_fix as rules
from app.models.diff import ChangedLines, added_lines
from app.models.finding import Finding, SkillResult
from app.models.fix import FixOutcome, code_fix_tests_ok, compare, unresolved
from app.models.repo_config import RepoConfig
from app.models.scope import parse_porcelain
from app.models.test_run import PytestRun
from app.sandbox.safe_read import read_regular_file
from app.worker import repo, scope
from app.worker.ai.claude_cli import AIErrorKind, ClaudeResult
from app.worker.coverage import read_sources
from app.worker.report import format_test_line

log = logging.getLogger(__name__)

NOT_LOGGED_IN: Final = "Not logged in"
MAX_GUARDED_BYTES: Final = 1024 * 1024


class AIEnvironmentError(RuntimeError):
    """claude를 실행할 수 없다 (명령 없음, 로그인 풀림). PR 탓이 아니라 환경 오류로 재시도한다."""


@dataclass
class CodeFixTools:
    """루프가 쓰는 외부 동작. 테스트에서는 가짜로 바꾼다."""

    run_ai: Callable[..., ClaudeResult]  # (worktree, prompt, *, model, max_budget_usd)
    recheck: Callable[[ChangedLines], SkillResult | None]  # 그 스킬의 재검사. 중단되면 None
    recheck_lint: Callable[[ChangedLines], SkillResult | None] | None  # lint가 꺼져 있으면 None
    run_tests: Callable[[], PytestRun]
    # (회차, 모델, 결과, 예산에서 뺀 비용)
    record_call: Callable[[int, str, ClaudeResult, float], None] = lambda *a: None
    is_stale: Callable[[], bool] = lambda: False
    # 재검사가 읽는 파이프라인 파일 (base의 ruff 설정 등, worktree 기준 경로). `.pipeline-out/`은
    # 범위 검사에서 빠지므로, AI가 바꿔 규칙을 끄지 못하게 회차마다 내용이 그대로인지 본다
    guarded: tuple[str, ...] = ()


@dataclass
class _Progress:
    calls: int = 0
    cost: float = 0.0
    models: list[str] = field(default_factory=list)
    heavy_used: bool = False
    consecutive_failures: int = 0
    feedback: list[str] = field(default_factory=list)
    last_detail: str = ""


class _Cancelled(Exception):
    pass


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


def environment_problem(result: ClaudeResult) -> str | None:
    """PR과 관계없이 claude를 쓸 수 없는 상태면 그 이유."""
    if result.error == AIErrorKind.LAUNCH:
        return f"claude를 실행하지 못함: {result.detail}"
    if NOT_LOGGED_IN in result.text or NOT_LOGGED_IN in result.detail:
        return "claude 로그인이 풀림"
    return None


def code_fix(
    worktree: Path,
    merge_base: str,
    skill: str,
    before: SkillResult,
    targets: Sequence[Finding],
    *,
    lint_before: SkillResult | None,
    test_before: PytestRun,
    config: RepoConfig,
    spent_usd: float,
    tools: CodeFixTools,
) -> FixOutcome | None:
    """targets를 AI로 고치고 검증한다. 새 커밋이 와서 중단되면 None.

    spent_usd는 이 PR에 이미 쓴 AI 비용이다. 예산은 `model.budget_per_pr_usd`.
    """
    allowed = {f.file for f in targets}
    guarded = {rel: read_regular_file(worktree / rel, max_bytes=MAX_GUARDED_BYTES)
               for rel in tools.guarded}  # fmt: skip
    outside = repo.outside_symlinks(worktree)
    if outside:  # 링크 폴더를 거친 쓰기는 worktree 밖으로 가고 범위 검사에도 안 보인다
        shown = ", ".join(f"`{p}`" for p in outside[:5])
        return FixOutcome(
            skill=skill, status="refused", detail=f"worktree 밖을 가리키는 심볼릭 링크: {shown}"
        )
    # AI를 처음 실행하기 직전에 한 번만 찍는다. 재시도 사이에 다시 찍지 않는다
    ignored_before = scope.snapshot_ignored(worktree)
    constants = module_constants(worktree, sorted(allowed))
    budget = config.model.budget_per_pr_usd
    p = _Progress()

    def outcome(status, **kwargs) -> FixOutcome:
        return FixOutcome(
            skill=skill, status=status, ai_calls=p.calls, ai_cost_usd=round(p.cost, 4),
            models=tuple(p.models), **kwargs,
        )  # fmt: skip

    try:
        for attempt in range(1, config.skills.fix.max_retries + 1):
            route = rules.choose_route(
                light=config.model.light, heavy=config.model.heavy,
                consecutive_failures=p.consecutive_failures, heavy_used=p.heavy_used,
                spent_usd=spent_usd + p.cost, budget_usd=budget,
            )  # fmt: skip
            if route.model is None:
                return outcome(
                    "budget",
                    detail=f"PR당 예산 ${budget:.2f} 중 ${spent_usd + p.cost:.2f}를 씀",
                )
            prompt = rules.build_prompt(skill, targets, constants=constants, feedback=p.feedback)
            result = tools.run_ai(
                worktree, prompt, model=route.model, max_budget_usd=route.max_budget_usd
            )
            charged = rules.charged_cost(result.cost_usd, route.max_budget_usd)
            p.calls += 1
            p.cost += charged
            p.models.append(route.model)
            p.heavy_used |= route.tier == "heavy"
            tools.record_call(attempt, route.model, result, charged)
            if result.permission_denials:
                log.warning(
                    "AI가 허용 범위 밖 도구 호출을 시도함 (%d건): %s",
                    len(result.permission_denials),
                    ", ".join(d.tool_name for d in result.permission_denials),
                )
            problem = environment_problem(result)
            if problem is not None:
                raise AIEnvironmentError(problem)
            if tools.is_stale():
                return None
            tampered = [
                rel for rel, data in guarded.items()
                if read_regular_file(worktree / rel, max_bytes=MAX_GUARDED_BYTES) != data
            ]  # fmt: skip
            if tampered:  # 다시 시도하지 않는다 (범위 밖 변경과 같다)
                return outcome(
                    "out_of_scope", detail=f"파이프라인 설정 파일이 바뀜: {', '.join(tampered)}"
                )
            final = _verify(worktree, merge_base, skill, before, targets, lint_before,
                            test_before, allowed, ignored_before, result, tools, p)  # fmt: skip
            if final is not None:
                return outcome(**final)
            if route.tier == "light":
                p.consecutive_failures += 1
    except _Cancelled:
        return None
    return outcome("ai_failed", detail=p.last_detail)


def _verify(
    worktree: Path,
    merge_base: str,
    skill: str,
    before: SkillResult,
    targets: Sequence[Finding],
    lint_before: SkillResult | None,
    test_before: PytestRun,
    allowed: set[str],
    ignored_before: frozenset[str],
    result: ClaudeResult,
    tools: CodeFixTools,
    p: _Progress,
) -> Mapping | None:
    """한 회차를 검증한다. 끝났으면(통과 또는 재시도 없는 실패) FixOutcome 인자, 다시 하면 None."""

    def retry(detail: str, feedback: list[str]) -> None:
        p.last_detail = detail
        p.feedback = feedback or [detail]

    if not result.ok:
        retry(f"AI 호출이 실패함 ({result.error})", [])
        return None
    checked = scope.check_scope(worktree, allowed, ignored_before)
    if not checked.ok:  # 샌드박스에서 아무것도 돌리기 전에 버린다. 다시 시도하지 않는다
        return {"status": "out_of_scope", "detail": checked.detail}
    entries = parse_porcelain(repo.status(worktree))
    if not entries:
        retry("AI가 아무 파일도 바꾸지 않음", ["아무 파일도 바꾸지 않았다. 위반을 고친다."])
        return None

    changed = added_lines(repo.working_diff(worktree, merge_base))
    after = tools.recheck(changed)
    if after is None:
        raise _Cancelled
    if not after.checked:
        return {"status": "unchecked", "detail": after.error or ""}
    left = unresolved(before.findings, targets, after)
    _, new = compare(before.findings, after)
    new_lint: list[Finding] = []
    if tools.recheck_lint is not None and lint_before is not None and lint_before.checked:
        lint_after = tools.recheck_lint(changed)
        if lint_after is None:
            raise _Cancelled
        if lint_after.checked:
            new_lint = compare(lint_before.findings, lint_after)[1]
    if left or new or new_lint:
        feedback = (
            rules.finding_feedback("아직 남은 위반", left)
            + rules.finding_feedback("새로 생긴 위반", new)
            + rules.finding_feedback("새로 생긴 lint 위반", new_lint)
        )
        retry(f"남은 위반 {len(left)}건, 새 위반 {len(new) + len(new_lint)}건", feedback)
        return None

    after_run = tools.run_tests()
    if after_run.cancelled:
        raise _Cancelled
    if not code_fix_tests_ok(test_before, after_run):
        line = format_test_line(after_run)
        failed = list(after_run.summary.failed_tests) if after_run.summary else []
        retry(f"고친 뒤 테스트: {line}", [f"고친 뒤 테스트: {line}"]
              + [f"실패한 테스트: {t}" for t in failed])  # fmt: skip
        return None

    changed_files = tuple(sorted({e.path for e in entries} & allowed))
    return {
        "status": "ready",
        "fixed": len(targets),
        "remaining": tuple(after.findings),
        "test_run": after_run,
        "changed_files": changed_files,
        "explanation": rules.explanation(result.text),
    }
