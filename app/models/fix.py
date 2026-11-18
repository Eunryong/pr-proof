"""수정안 판정 (docs/DESIGN.md F7·F8). 판정만 하고 파일을 바꾸지 않는다."""

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.models.finding import Finding, SkillResult
from app.models.scope import LINT_POLICY, StatusEntry, judge_scope
from app.models.scope import parse_porcelain as parse_porcelain  # 기존 호출처(graph)용 재공개
from app.models.test_run import PytestRun

FixStatus = Literal[
    "ready",  # 검증 통과. 제안할 수 있다
    "nothing_fixed",  # 적용할 수 있는 수정이 없었다
    "new_violations",  # 고친 뒤 원래 없던 위반이 생겼다
    "out_of_scope",  # 허용 범위 밖 파일이 바뀌었다
    "tests_changed",  # 고친 뒤 테스트 결과가 고치기 전과 다르다
    "unchecked",  # 고친 뒤 규칙 검사를 하지 못했다
    "ai_failed",  # AI 수정이 정해진 회차 안에 검증을 통과하지 못했다
    "budget",  # PR당 AI 예산을 다 써서 멈췄다
    "refused",  # AI를 실행하지 않았다 (worktree 밖을 가리키는 심볼릭 링크 등)
    "secret",  # AI 수정안(diff 또는 변경 설명)에 비밀값으로 보이는 내용이 있어 버렸다
]


@dataclass(frozen=True)
class FixOutcome:
    skill: str
    status: FixStatus
    fixed: int = 0  # 해소한 위반 수
    remaining: tuple[Finding, ...] = ()  # 고친 뒤에도 남은 PR의 위반
    test_run: PytestRun | None = None  # 고친 뒤 테스트
    changed_files: tuple[str, ...] = ()
    formatted_files: tuple[str, ...] = ()  # ruff format으로 바뀐 파일
    format_skipped: str = ""  # 포맷하지 않은 이유
    patch_path: Path | None = None  # 수정안 diff (제안 PR에 쓴다)
    detail: str = ""  # 버린 이유 등
    # AI 수정 (static·structure)만 쓴다
    explanation: str = ""  # AI가 쓴 변경 설명. GitHub에 올리기 전에 비밀값 검사를 거친다
    ai_calls: int = 0
    ai_cost_usd: float = 0.0  # 추정 비용. 비용을 모르는 호출은 호출당 상한으로 센다
    models: tuple[str, ...] = ()  # 회차별 모델

    @property
    def ready(self) -> bool:
        return self.status == "ready"


def out_of_scope(entries: Iterable[StatusEntry], allowed: Iterable[str]) -> list[str]:
    """lint 수정의 허용 범위 밖 변경 ("XY 경로" 목록). LINT_POLICY로 판정한다."""
    return [v.label for v in judge_scope(entries, allowed, LINT_POLICY).violations]


def _key(f: Finding) -> tuple[str, str, str]:
    return (f.file, f.code, f.message)  # 줄 번호는 수정으로 밀리므로 비교에 쓰지 않는다


def compare(before: Sequence[Finding], after: SkillResult) -> tuple[int, list[Finding]]:
    """(해소한 수, 새로 생긴 위반). 같은 파일·규칙·메시지의 개수로 비교한다."""
    before_count = Counter(_key(f) for f in before)
    after_count = Counter(_key(f) for f in after.findings)
    new = []
    seen: Counter = Counter()
    for f in after.findings:
        seen[_key(f)] += 1
        if seen[_key(f)] > before_count[_key(f)]:
            new.append(f)
    fixed = sum((before_count - after_count).values())
    return fixed, new


def same_test_result(before: PytestRun, after: PytestRun) -> bool:
    """고친 뒤 테스트가 고치기 전과 같은 상태(통과 또는 테스트 없음)인지. 개수도 같아야 한다."""
    if before.status != after.status or not after.allows_lint_fix:
        return False
    if before.summary is None or after.summary is None:
        return before.summary is after.summary or after.status == "no_tests"
    # 불안정한 테스트는 재실행에서 통과한 수까지 센다
    return (before.passed_count, before.summary.total) == (after.passed_count, after.summary.total)


def code_fix_tests_ok(before: PytestRun, after: PytestRun) -> bool:
    """AI 수정 뒤 테스트: 모두 통과하고(불안정한 테스트 없이) 수정 전과 같은 수가 돌았다."""
    if not after.allows_code_fix or before.summary is None or after.summary is None:
        return False
    return (after.passed_count, after.summary.total) == (before.passed_count, before.summary.total)


def unresolved(
    before: Sequence[Finding], targets: Sequence[Finding], after: SkillResult
) -> list[Finding]:
    """고치기로 한 위반 중 고친 뒤에도 남은 것. 파일·규칙·메시지의 개수로 비교한다.

    같은 키의 위반이 여럿이고 그중 일부만 대상이면, 대상 수만큼 줄어야 해소로 본다.
    """
    before_count = Counter(_key(f) for f in before)
    after_count = Counter(_key(f) for f in after.findings)
    needed = Counter(_key(f) for f in targets)
    left = []
    for f in targets:
        k = _key(f)
        if after_count[k] > before_count[k] - needed[k]:
            left.append(f)
    return left


FixBlocker = Literal["no_fixable", "tests"]


def lint_fix_blocker(lint: SkillResult | None, run: PytestRun) -> FixBlocker | None:
    """`/fix lint`를 제안할 수 없는 이유. 제안할 수 있으면 None (DESIGN.md F8 표)."""
    if lint is None or not lint.findings:
        return "no_fixable"
    if lint.fixable_count == 0:
        return "no_fixable"
    if not run.allows_lint_fix:
        return "tests"
    return None
