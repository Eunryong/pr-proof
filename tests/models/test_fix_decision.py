"""models/fix_decision.py: 파이프라인이 정하는 표와 모델 답의 위반 집합 맞추기."""

from dataclasses import replace
from typing import Final

import pytest

from app.models.fix_decision import FixDecision, SafetyNet, decide_by_table, plan, reconcile
from app.models.violation import Violation

V: Final = Violation(
    id="v1", rule_id="R-1", file="orders/a.py", line=3, message="m", found_by="checker",
    line_text="print(x)", covered=True,
)  # fmt: skip
PASSED: Final = SafetyNet("passed")


def test_covered_violation_with_passing_tests_goes_to_the_model() -> None:
    assert decide_by_table(V, "model", PASSED) is None


@pytest.mark.parametrize(
    ("violation", "rule_fix", "tests", "reason", "why"),
    [
        (replace(V, rejected_in=15), "model", PASSED, "rejected_before", "이전에 거절됨 (#15)"),
        (V, "none", PASSED, "notify_only", "알리기만 하는 규칙"),
        (replace(V, unit="file", line=0), "model", PASSED, "notify_only", "알리기만 하는 규칙"),
        (V, "model", SafetyNet("failed"), "tests_failed", "테스트가 실패함"),
        (V, "model", SafetyNet("timeout"), "tests_failed", "테스트가 시간 안에 끝나지 않음"),
        (V, "model", SafetyNet("unrunnable"), "tests_failed", "테스트를 돌리지 못함"),
        (V, "model", SafetyNet("no_tests"), "no_safety_net", "안전망이 없음 (테스트가 없음)"),
        (V, "model", SafetyNet("passed", flaky=True), "no_safety_net",
         "안전망이 없음 (불안정한 테스트가 있음)"),
        (replace(V, covered=False), "model", PASSED, "not_covered", "테스트가 지나가지 않음"),
        (replace(V, covered=None), "model", PASSED, "coverage_unknown", "커버리지를 알 수 없음"),
    ],
)  # fmt: skip
def test_table_skips_with_the_reason(violation, rule_fix, tests, reason, why) -> None:
    decision = decide_by_table(violation, rule_fix, tests)
    assert decision == FixDecision(violation.id, "skip", "pipeline", reason=reason, why=why)


def test_first_matching_row_wins() -> None:
    # 거절 기록이 테스트 실패보다 먼저, 테스트 실패가 기계적 수정보다 먼저다
    rejected = replace(V, rejected_in=7, mechanical_fix=True)
    assert decide_by_table(rejected, "none", SafetyNet("failed")).reason == "rejected_before"
    mechanical = replace(V, mechanical_fix=True)
    assert decide_by_table(mechanical, "mechanical", SafetyNet("failed")).reason == "tests_failed"


@pytest.mark.parametrize("tests", [PASSED, SafetyNet("no_tests"), SafetyNet("passed", flaky=True)])
def test_mechanical_fix_does_not_need_a_safety_net(tests: SafetyNet) -> None:
    v = replace(V, mechanical_fix=True, covered=None)
    assert decide_by_table(v, "mechanical", tests) == FixDecision(
        "v1", "fix", "pipeline", method="mechanical"
    )


def test_coverage_is_ignored_when_the_repo_does_not_require_it() -> None:
    assert (
        decide_by_table(replace(V, covered=False), "model", PASSED, require_covered=False) is None
    )


def test_plan_splits_decided_and_model_and_unknown_rule_is_notify_only() -> None:
    other = replace(V, id="v2", rule_id="R-none")
    unknown = replace(V, id="v3", rule_id="R-unknown")
    planned = plan([V, other, unknown], {"R-1": "model", "R-none": "none"}, PASSED)
    assert planned.for_model == (V,)
    assert [(d.violation_id, d.reason) for d in planned.decided] == [
        ("v2", "notify_only"), ("v3", "notify_only"),
    ]  # fmt: skip


def test_model_cannot_drop_add_or_repeat_violations() -> None:
    asked = [V, replace(V, id="v2"), replace(V, id="v3")]
    answers = [
        FixDecision("v3", "fix", "model", method="model"),
        FixDecision("v-made-up", "fix", "model", method="model"),
        FixDecision("v1", "skip", "model", reason="risky", why="영향이 넓다"),
        FixDecision("v1", "fix", "model", method="model"),
    ]
    result = reconcile(asked, answers)
    assert [d.violation_id for d in result] == ["v1", "v2", "v3"]
    assert result[0].reason == "risky"  # 같은 id의 두 번째 답은 버린다
    assert result[1] == FixDecision(
        "v2", "skip", "pipeline", reason="undecided", why="모델이 답하지 않음"
    )
    assert result[2].action == "fix"
