"""app/worker/pr_check_report_pure.py: 결과 코멘트의 PR 단위 확인 부분 (4.5 끝, 4.9, T10)."""

from typing import Final

from app.worker import doc_rules_report, pr_check_report_pure
from app.worker.doc_rules_pure import CallUsage, Found, RuleUnchecked
from app.worker.rule_pure import CheckMethod, Rule, RuleSource
from app.worker.rules.pr_check_pure import PrChecks, PrPattern, PrRuleResult, PrUsage, PrViolation

SHA: Final = "a" * 40


def _rule(rid: str, text: str) -> Rule:
    return Rule(id=rid, text=text, source=RuleSource("README.md", "기여", "0"), origin="document",
                scope=(), check=CheckMethod("none"), fix="none", target="pr")  # fmt: skip


COMMITS: Final = _rule("R-c", "커밋 메시지는 feat: 로 시작한다.")
BODY: Final = _rule("R-b", "PR 본문에 테스트 방법을 적는다.")
PATTERN: Final = PrPattern("commit_subject", "must_match", "^feat: ")


def _checks(*results: PrRuleResult, violations=(), **kw) -> PrChecks:
    return PrChecks(tuple(results), tuple(violations), **kw)


def test_title_counts_pr_violations_apart_from_code() -> None:
    found = _checks(PrRuleResult(COMMITS, "checked", "pattern", PATTERN),
                    violations=[PrViolation("R-c", "commit", "pattern", SHA, "x")])  # fmt: skip
    assert pr_check_report_pure.title_part(found) == " · ⚠️ PR 단위 위반 1건 (규칙 1개)"
    clean = _checks(PrRuleResult(COMMITS, "checked", "pattern", PATTERN),
                    PrRuleResult(BODY, "not_applicable", "model", PrPattern("pr_body")),
                    PrRuleResult(_rule("R-x", "브랜치 규칙이다."), "unchecked",
                                 kind="cap"))  # fmt: skip
    assert pr_check_report_pure.title_part(clean) == (
        " · PR 단위 규칙 2개 위반 없음 · PR 단위 규칙 1개 확인하지 못함"
    )


def test_author_text_stays_inside_code_spans() -> None:
    """커밋 제목, 모델의 이유는 작성자가 고를 수 있는 글이다. 백틱으로 스팬을 닫거나 멘션을 걸지
    못한다 (T10)."""
    hostile = "x` @owner <b>보라</b> ``"
    checks = _checks(
        PrRuleResult(COMMITS, "checked", "pattern", PATTERN),
        violations=[PrViolation("R-c", "commit", "pattern", SHA, hostile, f"이유 {hostile}")],
    )
    lines = pr_check_report_pure.section(checks)
    (line,) = [x for x in lines if x.startswith("  - 커밋")]
    assert line.startswith(f"  - 커밋 `{SHA[:7]}` ```")
    for part in line.split(" · ")[1:] + [line.split("` ", 1)[1].split(" · ")[0]]:
        fence = part[: len(part) - len(part.lstrip("`"))]
        assert len(fence) >= 3 and part.rstrip().endswith(fence), part


def test_regex_is_shown_with_its_spaces() -> None:
    checks = _checks(PrRuleResult(COMMITS, "checked", "pattern", PATTERN))
    text = "\n".join(pr_check_report_pure.section(checks))
    assert 'PR 패턴 (커밋 제목이 `"^feat: "`에 맞아야 함)' in text


def test_body_violation_without_quote_says_something_is_missing() -> None:
    checks = _checks(PrRuleResult(BODY, "checked", "model", PrPattern("pr_body"), "model_said"),
                     violations=[PrViolation("R-b", "pr_body", "model", why="없음")])  # fmt: skip
    text = "\n".join(pr_check_report_pure.section(checks))
    assert "  - PR 본문 (빠진 내용) · `없음`" in text
    assert pr_check_report_pure.RECHECK_HINT in text
    assert "모델 판단 (PR 본문, 글의 뜻을 읽어야 한다고 모델이 답함)" in text


def test_every_state_has_its_line() -> None:
    checks = _checks(
        PrRuleResult(COMMITS, "checked", "pattern", PATTERN, partial="커밋이 250개를 넘어"),
        PrRuleResult(BODY, "left", "model", PrPattern("pr_body"), kind="cap", detail="PR 상한"),
        PrRuleResult(_rule("R-f", "제목 규칙이다."), "left", "model", PrPattern("pr_title"),
                     kind="model_failed", detail="시간 초과"),
        PrRuleResult(_rule("R-n", "승인 규칙이다."), "unchecked", kind="plan_none",
                     detail="사람의 절차"),
        commits=3, skipped_merge=1, skipped_pipeline=1,
        usage=PrUsage(plan_calls=2, judge_calls=1, cached=1, dropped=2, input_tokens=5,
                      output_tokens=6, models=("sonnet",)),
    )  # fmt: skip
    text = "\n".join(pr_check_report_pure.section(checks))
    assert "커밋 3개 (보지 않음: merge 커밋 1개, 파이프라인 수정안 커밋 1개)" in text
    assert "모델 호출 3회 (확인 방법 정하기 2, 판단 1)" in text and "버린 답 2건" in text
    assert "- `R-c` · `커밋이 250개를 넘어`" in text
    assert "모델 호출 상한에 걸려 처리하지 못함: PR 단위 판단 1개 규칙 (`PR 상한`)" in text
    assert "확인하지 못한 PR 단위 규칙 2개:" in text
    assert "PR 사실로 확인할 수 없는 규칙이라고 모델이 답함: `사람의 절차`" in text


def test_unreadable_facts_are_said_once() -> None:
    checks = _checks(PrRuleResult(COMMITS, "unchecked", kind="no_facts", detail="404"),
                     note="PR 사실을 읽지 못함: 404")  # fmt: skip
    text = "\n".join(pr_check_report_pure.section(checks))
    assert "PR 사실을 읽지 못해 확인하지 않음: `PR 사실을 읽지 못함: 404`" in text
    assert "커밋 0개" not in text


def test_rules_section_replaces_the_unchecked_list_when_pr_checks_ran() -> None:
    found = Found((RuleUnchecked(COMMITS, "pr", "PR 규칙"),), (), 0, CallUsage(0, 10, 0, (), 0, 0))
    before = "\n".join(doc_rules_report.section(found, {}))
    assert "PR 단위로 확인할 규칙 1개" in before
    assert (
        doc_rules_report.title(found)
        == "**규칙:** 검사한 규칙 없음 (읽은 규칙 1개) · PR 단위 규칙 1개"
    )
    checks = _checks(PrRuleResult(COMMITS, "checked", "pattern", PATTERN))
    after = "\n".join(doc_rules_report.section(found, {}, pr=checks))
    assert "PR 단위로 확인할 규칙" not in after and "**PR 단위 확인 (알리기만" in after
    assert doc_rules_report.title(found, pr=checks).endswith("· PR 단위 규칙 1개 위반 없음")
