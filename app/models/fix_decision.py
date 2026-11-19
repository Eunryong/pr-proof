"""수정 판단 (docs/DESIGN.md 4.6, 7.4). 데이터와, 파이프라인이 먼저 정하는 결정적 표.

위반마다 `fix` 또는 `skip` 하나로 끝난다. 표에 맞는 위반은 여기서 정하고, 맞지 않는 위반만
모델에 넘긴다. 모델 호출과 위반 집합 비교는 worker 쪽이 한다.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from app.models.test_run import RunStatus
from app.models.violation import Violation

SkipReason = Literal[
    "rejected_before", "notify_only", "tests_failed", "no_safety_net", "not_covered",
    "coverage_unknown", "team_pattern", "risky", "call_limit", "undecided",
]  # fmt: skip
FixMethod = Literal["mechanical", "model", "none"]

_TESTS_BROKEN: Final[Mapping[str, str]] = {
    "failed": "테스트가 실패함",
    "timeout": "테스트가 시간 안에 끝나지 않음",
    "unrunnable": "테스트를 돌리지 못함",
    "cancelled": "테스트가 중단됨",
}


@dataclass(frozen=True)
class FixDecision:
    violation_id: str
    action: Literal["fix", "skip"]
    decided_by: Literal["pipeline", "model"]
    method: Literal["mechanical", "model", ""] = ""
    reason: SkipReason | None = None
    why: str = ""  # 코멘트에 보이는 한 줄
    evidence: tuple[int, ...] = ()  # 참고한 과거 기록 id


@dataclass(frozen=True)
class SafetyNet:
    """수정 판단이 보는 테스트 결과."""

    status: RunStatus
    flaky: bool = False  # 불안정한 테스트가 있다. 모델 수정의 안전망으로 쓰지 않는다


def _skip(v: Violation, reason: SkipReason, why: str) -> FixDecision:
    return FixDecision(v.id, "skip", "pipeline", reason=reason, why=why)


def decide_by_table(
    violation: Violation, rule_fix: FixMethod, tests: SafetyNet, *, require_covered: bool = True
) -> FixDecision | None:
    """파이프라인이 정하는 표 (4.6). 위에서부터 처음 맞는 줄을 쓴다. 맞는 줄이 없으면 None이고
    그 위반은 모델이 정한다.

    rule_fix는 그 위반의 규칙이 가진 수정 방법이다. 파일 단위 위반(이름·경로)은 고치는 영향이 넓어
    알리기만 한다.
    """
    v = violation
    if v.rejected_in is not None:
        return _skip(v, "rejected_before", f"이전에 거절됨 (#{v.rejected_in})")
    if rule_fix == "none" or v.unit == "file":
        return _skip(v, "notify_only", "알리기만 하는 규칙")
    if tests.status in _TESTS_BROKEN:
        return _skip(v, "tests_failed", _TESTS_BROKEN[tests.status])
    if v.mechanical_fix:  # 테스트 상태는 passed 또는 no_tests다
        return FixDecision(v.id, "fix", "pipeline", method="mechanical")
    if tests.status == "no_tests" or tests.flaky:
        why = "테스트가 없음" if tests.status == "no_tests" else "불안정한 테스트가 있음"
        return _skip(v, "no_safety_net", f"안전망이 없음 ({why})")
    if require_covered and v.covered is False:
        return _skip(v, "not_covered", "테스트가 지나가지 않음")
    if require_covered and v.covered is None:
        return _skip(v, "coverage_unknown", "커버리지를 알 수 없음")
    return None


@dataclass(frozen=True)
class Planned:
    """표를 적용한 결과. decided는 끝난 판단, for_model은 모델이 정할 위반이다."""

    decided: tuple[FixDecision, ...]
    for_model: tuple[Violation, ...]


def plan(
    violations: Sequence[Violation],
    rule_fix: Mapping[str, FixMethod],
    tests: SafetyNet,
    *,
    require_covered: bool = True,
) -> Planned:
    """위반마다 표를 적용한다. rule_fix는 {규칙 id: 수정 방법}이고, 없는 규칙은 알리기만 한다."""
    decided, for_model = [], []
    for v in violations:
        decision = decide_by_table(
            v, rule_fix.get(v.rule_id, "none"), tests, require_covered=require_covered
        )
        if decision is None:
            for_model.append(v)
        else:
            decided.append(decision)
    return Planned(tuple(decided), tuple(for_model))


def reconcile(
    asked: Sequence[Violation], answers: Sequence[FixDecision]
) -> tuple[FixDecision, ...]:
    """모델의 답을 넘긴 위반 집합에 맞춘다 (4.6). 모델은 위반 목록을 바꿀 수 없다.

    넘기지 않은 id의 답은 버리고, 같은 id의 답이 둘이면 처음 것만 쓰고, 답이 없는 위반은
    `skip`(모델이 답하지 않음)으로 채운다. 결과는 asked와 같은 순서, 같은 수다.
    """
    by_id: dict[str, FixDecision] = {}
    wanted = {v.id for v in asked}
    for answer in answers:
        if answer.violation_id in wanted:
            by_id.setdefault(answer.violation_id, answer)
    return tuple(
        by_id.get(v.id)
        or FixDecision(v.id, "skip", "pipeline", reason="undecided", why="모델이 답하지 않음")
        for v in asked
    )
