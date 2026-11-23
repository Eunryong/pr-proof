"""app/worker/fix/feedback_fix_pure.py: 받아들인 코멘트를 수정 작성의 틀에 태우는 자료 (4.11)."""

from app.worker.core.violation_pure import Violation
from app.worker.feedback_pure import Item
from app.worker.feedback_triage_pure import Triage
from app.worker.fix.feedback_fix_pure import Accepted, feedback_found, fix_targets
from app.worker.rules.doc_rules_pure import CallUsage, Found, outcomes_by_rule


def _accepted(request="b가 0이면 ValueError") -> Accepted:
    item = Item(kind="review_comment", item_id=101, thread_id="T", login="Copilot",
                association="NONE", path="calc.py", line=2, body="0으로 나누기\n위험",
                replies=(), outdated=False)  # fmt: skip
    return Accepted(item, Triage(101, "accept", "w", ("calc.py", "util.py"), request))


def test_targets_are_one_per_file_and_the_request_is_the_rule_text() -> None:
    lines = {"calc.py": ["def div(a, b):", "    return a / b"], "util.py": ["x = 1"]}
    violations, rules = fix_targets([_accepted()], lines)
    assert [(v.file, v.line, v.line_text, v.found_by) for v in violations] == [
        ("calc.py", 2, "return a / b", "feedback"), ("util.py", 0, "", "feedback"),
    ]  # fmt: skip
    assert violations[0].message == "코멘트 원문: 0으로 나누기 위험"
    (rule,) = rules
    assert (rule.id, rule.text) == ("feedback-101", "b가 0이면 ValueError")
    other, _ = fix_targets([_accepted("다른 할 일")], lines)
    assert other[0].id != violations[0].id  # 할 일이 바뀌면 저장 키도 바뀐다


def test_found_keeps_the_rules_checks_violations() -> None:
    violations, rules = fix_targets([_accepted()], {})
    old = Violation(id="v", rule_id="R", file="calc.py", line=9, message="m", found_by="checker")
    base = Found((), (old,), 0, CallUsage(0, 0, 0, (), 0, 0))
    found = feedback_found(base, violations, rules)
    assert found.violations[0] is old and len(found.violations) == 3
    assert outcomes_by_rule(found)["feedback-101"].rule.text == "b가 0이면 ValueError"
    assert feedback_found(None, violations, rules).violations == tuple(violations)
