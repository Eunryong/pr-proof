"""이유 글 한국어화 캐싱(checker.reason_ko) 및 위반별 이유 노출·빈 why 보정 테스트."""

from app.worker.core.checker_pure import Checker
from app.worker.core.rule_pure import CheckMethod, Rule, RuleSource
from app.worker.core.violation_pure import Violation
from app.worker.report import doc_rules_report
from app.worker.rules.doc_rules_pure import CallUsage, CheckerRef, Found, RuleChecked, RuleJudged
from app.worker.rules.judge_pure import Asked, Hunk, parse_answer, verify_answer

RULE = Rule(
    id="R-readme-11111111",
    text="`orders/` 아래 모듈은 `print`를 쓰지 않는다.",
    source=RuleSource("README.md", "코드 규칙 > 출력", "a" * 40),
    origin="document",
    scope=("orders/**",),
    check=CheckMethod("checker"),
    fix="model",
)
REF = CheckerRef(key="k1", id="c1", kind="ast-grep", cached=False)


def _found(*outcomes, violations=()) -> Found:
    return Found(
        outcomes=outcomes,
        violations=violations,
        disabled=0,
        usage=CallUsage(0, 10, 0, (), 0, 0),
    )


def test_checker_reason_ko_overrides_raw_english_tool_message() -> None:
    checker = Checker(
        id="c1",
        rule_id=RULE.id,
        rule_text_hash="h",
        kind="ast-grep",
        body="id: x",
        examples=(),
        engine_version="v1",
        reason_ko="프로덕션 코드에서 `print` 대신 `logging` 로거를 사용해야 합니다.",
    )
    v = Violation(
        id="v1",
        rule_id=RULE.id,
        file="orders/a.py",
        line=10,
        message="`print` found",
        found_by="checker",
    )
    text = "\n".join(
        doc_rules_report.section(_found(RuleChecked(RULE, REF), violations=(v,)), {"k1": checker})
    )
    assert "프로덕션 코드에서 `print` 대신 `logging` 로거를 사용해야 합니다." in text
    assert "`print` found" not in text


def test_distinct_violation_messages_are_shown_inline_per_line() -> None:
    v1 = Violation(
        id="v1",
        rule_id=RULE.id,
        file="orders/a.py",
        line=10,
        message="정의부에서 실수 나눗셈 `/` 사용",
        found_by="model",
    )
    v2 = Violation(
        id="v2",
        rule_id=RULE.id,
        file="orders/a.py",
        line=25,
        message="호출부에서 결과를 다시 `/`로 나눔",
        found_by="model",
    )
    text = "\n".join(
        doc_rules_report.section(_found(RuleJudged(RULE, "의미"), violations=(v1, v2)), {})
    )
    assert "정의부에서 실수 나눗셈 `/` 사용" in text
    assert "호출부에서 결과를 다시 `/`로 나눔" in text


def test_judge_verify_falls_back_to_basis_when_why_is_empty() -> None:
    hunk = Hunk("orders/a.py", 1, ("print(1)",), frozenset({1}))
    asked = [Asked(hunk, (RULE,))]
    answer = parse_answer(
        {
            "violations": [
                {
                    "rule_id": RULE.id,
                    "file": "orders/a.py",
                    "line": 1,
                    "quote": "print(1)",
                    "why": "  ",
                }
            ],
            "undecided": [],
            "pairs": [
                {
                    "hunk": 1,
                    "rule_id": RULE.id,
                    "basis": "orders/a.py:1에서 print를 직접 호출함",
                    "keep": False,
                }
            ],
        }
    )
    verified = verify_answer(asked, answer)
    flags = verified.kept[(0, RULE.id)]
    assert len(flags) == 1
    assert flags[0].why == "orders/a.py:1에서 print를 직접 호출함"
