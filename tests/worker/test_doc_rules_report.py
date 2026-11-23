"""app/worker/doc_rules_report.py: 결과 코멘트의 "규칙:" 줄과 규칙 절. 믿지 않는 글의 처리와 길이
상한."""

import re
from dataclasses import replace
from typing import Final

from app.worker import doc_rules_report, report
from app.worker.core.checker_pure import Checker
from app.worker.core.markdown_pure import inline_code
from app.worker.core.rule_pure import CheckMethod, Example, Rule, RuleSource, UnreadDoc
from app.worker.core.violation_pure import Violation
from app.worker.doc_rules_pure import (
    CallUsage,
    CheckerRef,
    Found,
    NoRuleDocs,
    NoRulesFound,
    NotRun,
    RuleChecked,
    RuleJudged,
    RuleUnchecked,
    RunJudged,
)
from app.worker.doc_rules_report import NO_RULES, FixView, section, title
from app.worker.fix.fix_decision_pure import Decided, FixDecision
from app.worker.fix.fix_pure import MechanicalFix
from app.worker.fix.model_fix_pure import ModelFix, Verification
from app.worker.fix.proposal_pure import ProposalResult
from app.worker.rules.judge_pure import JudgeUsage
from app.worker.testing.test_run_pure import PytestRun, ReportSummary

USAGE: Final = CallUsage(calls=3, limit=14, cache_hits=1, models=("claude-haiku-5-5",),
                         input_tokens=3000, output_tokens=300)  # fmt: skip
RULE: Final = Rule(
    id="R-readme-1", text="`orders/` 아래 모듈은 `print`를 쓰지 않는다.",
    source=RuleSource("README.md", "코드 규칙 > 출력", "abc"), origin="document",
    scope=("orders/**",), check=CheckMethod("checker"), fix="model",
)  # fmt: skip
REF: Final = CheckerRef(key="k1", id="c" * 64, kind="ast-grep", cached=False)
CHECKER: Final = Checker(
    id="c" * 64, rule_id="R-readme-1", rule_text_hash="h", kind="ast-grep",
    body="id: no-print\nlanguage: python\nrule:\n  pattern: print($$$A)\n",
    examples=(Example("print(1)\n", (1,), "document"), Example("log(1)\n", (), "model")),
    engine_version="ast-grep-0.51.3", model="claude-haiku-5-5",
)  # fmt: skip


def _violation(file: str = "orders/a.py", line: int = 2, rule: str = "R-readme-1") -> Violation:
    return Violation(id=f"{file}{line}", rule_id=rule, file=file, line=line, message="m",
                     found_by="checker", line_text="print(x)")  # fmt: skip


def _found(*outcomes, violations=(), disabled: int = 0) -> Found:
    return Found(outcomes=outcomes, violations=violations, disabled=disabled, usage=USAGE)


def _text(result, checkers=None) -> str:
    """ "규칙:" 줄과 그 아래 규칙 절. 코멘트에서는 그 사이에 테스트 상세가 들어간다."""
    return "\n".join(["", title(result) or "", *section(result, checkers or {})]).rstrip("\n")


def _outside_code(text: str) -> str:
    """코드 울타리와 코드 스팬을 뺀 글. 마크다운·HTML·멘션이 동작하는 부분이다."""
    text = re.sub(r"(?ms)^(`{3,})[^\n]*\n.*?^\1$", "", text)
    return re.sub(r"(`+)(?!`).*?(?<!`)\1(?!`)", "", text)


def test_violation_lists_location_quote_source_and_checker() -> None:
    text = _text(_found(RuleChecked(RULE, REF), violations=(_violation(),)), {"k1": CHECKER})
    assert "**규칙:** ⚠️ 위반 1건 (규칙 1개)" in text
    assert "- `R-readme-1` `` `orders/` 아래 모듈은 `print`를 쓰지 않는다. `` · 1건" in text
    assert "  - `orders/a.py:2`" in text
    assert doc_rules_report.MANY_VIOLATIONS_NOTE not in text
    assert "  출처: `README.md > 코드 규칙 > 출력` · 모델이 쓴 검사기 (ast-grep)" in text
    assert "```yaml\nid: no-print\nlanguage: python\nrule:\n  pattern: print($$$A)\n```" in text
    assert "예시: 문서 1개, 모델이 쓴 것 1개" in text
    assert "모델이 쓴 예시, 정상:\n\n```python\nlog(1)\n```" in text
    assert "print(1)" not in text  # 문서의 예시는 문서에 있으므로 다시 싣지 않는다
    assert "README의 그 문장" not in text and "문장을 고치" not in text
    shown, _, folded = text.partition(doc_rules_report.DETAILS_SUMMARY)
    assert "위반:" in shown and "읽은 규칙 1개" in folded
    assert "모델 호출 3회 / 상한 14회 (규칙 읽기와 검사기 작성) · `claude-haiku-5-5` · " \
           "저장된 결과 1건 사용 · 토큰 입력 3000, 출력 300" in text  # fmt: skip


def test_checker_without_violation_has_no_body() -> None:
    text = _text(_found(RuleChecked(RULE, REF)), {"k1": CHECKER})
    assert "**규칙:** ✅ 위반 없음 · 규칙 1개를 확인함 (도구·검사기 1개)" in text
    assert "<summary>모델이 쓴 검사기" not in text and "읽은 규칙 1개" in text
    shown, _, _ = text.partition("<details>")
    assert shown.strip() == "**규칙:** ✅ 위반 없음 · 규칙 1개를 확인함 (도구·검사기 1개)"


def test_untrusted_text_cannot_escape_code() -> None:
    nasty = "@team <img src=x onerror=alert(1)> ``` [x](http://e) **b**"
    rule = replace(RULE, text=nasty, source=RuleSource("README.md", "<b>@sec</b>", "a"))
    body = "id: x\n````\n@team <script>\n````\n"
    checker = replace(CHECKER, body=body,
                      examples=(Example("x = '``````'\n@a <i>\n", (1,), "model"),))  # fmt: skip
    found = _found(
        RuleChecked(rule, REF), RuleUnchecked(replace(rule, id="R-readme-2"), "run_failed", nasty),
        violations=(_violation(file="orders/`@x`<b>.py"),),
    )  # fmt: skip
    text = _text(found, {"k1": replace(checker, rule_id=rule.id)})
    outside = _outside_code(text)
    assert "@" not in outside
    assert re.sub(r"</?(details|summary|code)>", "", outside).count("<") == 0
    assert "`````yaml\n" + body.rstrip("\n") + "\n`````" in text
    assert "```````python\n" in text


def test_inline_code_collapses_lines_and_outgrows_backticks() -> None:
    assert inline_code("a\n\nb") == "`a b`"
    assert inline_code("x ``y`` z") == "```x ``y`` z```"
    assert inline_code("`a`") == "`` `a` ``"
    assert inline_code("a" * 10, limit=3) == "`aaa…`"


def test_inferred_exclude_is_shown_with_reason() -> None:
    rule = replace(RULE, scope=(), exclude=("tests/**", "scripts/*.py"),
                   exclude_reason="테스트와 스크립트는 <제품 코드>가 아님")  # fmt: skip
    text = _text(_found(RuleChecked(rule, REF)))
    assert ("  범위 추정: `tests/**`, `scripts/*.py` 제외 (문서에 범위 없음) · "
            "`테스트와 스크립트는 <제품 코드>가 아님`") in text  # fmt: skip
    assert "범위: 저장소 전체" in text
    assert "범위 추정" not in _text(_found(RuleChecked(RULE, REF)))


def test_unchecked_rules_are_grouped_by_reason() -> None:
    outcomes = (
        RuleUnchecked(replace(RULE, id="R-1"), "no_checker", "3회 안에 예시를 통과하지 못함"),
        RuleUnchecked(replace(RULE, id="R-6", target="pr"), "pr", "커밋 메시지의 규칙"),
        RuleUnchecked(replace(RULE, id="R-3"), "cap", "호출 상한: 규칙 묶음 상한 (3/3회 사용)"),
        RuleUnchecked(replace(RULE, id="R-4"), "cap", "호출 상한: 규칙 묶음 상한 (3/3회 사용)"),
        RuleUnchecked(replace(RULE, id="R-5"), "tool_unavailable", "도구를 준비하지 못함: x"),
    )
    text = _text(_found(*outcomes, disabled=2))
    assert "**규칙:** 확인한 규칙 없음 (읽은 규칙 5개)" in text
    assert "`rules.disabled`로 끈 규칙 2개" in text.partition(doc_rules_report.DETAILS_SUMMARY)[2]
    assert (
        "읽었지만 아직 검사하지 못하는 규칙 1개 (예시를 통과하는 검사기가 없음):\n- `R-1`" in text
    )
    assert "이번 검사에서 검사하지 못한 규칙 1개 (검사하지 못함 (도구 준비 실패) 1개):" in text
    assert ("모델 호출 상한에 걸려 처리하지 못함: 검사기 작성 2개 규칙 "
            "(`호출 상한: 규칙 묶음 상한 (3/3회 사용)`). 다음 검사") in text  # fmt: skip
    assert ("PR 단위 확인을 마치지 못한 규칙 1개 (커밋 메시지, PR 본문, 브랜치, 바뀐 파일 "
            "종류. 다음 검사나 `/recheck`가 확인합니다):\n- `R-6`") in text  # fmt: skip
    assert "검사하지 않음" not in text and "사람이 확인" not in text


def test_title_counts_only_rules_it_confirmed() -> None:
    outcomes = (
        RuleChecked(RULE, REF),
        RuleUnchecked(replace(RULE, id="R-1"), "no_checker", "3회 안에 예시를 통과하지 못함"),
    )
    assert "**규칙:** ✅ 위반 없음 · 규칙 2개 중 1개를 확인함 (도구·검사기 1개)" in _text(
        _found(*outcomes)
    )


def test_old_human_outcome_from_a_checkpoint_is_shown_as_not_confirmed() -> None:
    """2026-11-22 전의 체크포인트에 남은 "human" 결과는 확인한 것으로 세지 않고, "검사하지 않음"이
    아니라 다시 확인할 방법과 함께 보인다."""
    old = RuleUnchecked(replace(RULE, id="R-2"), "human", "절차")  # type: ignore[arg-type]
    text = _text(_found(RuleChecked(RULE, REF), old))
    assert "규칙 2개 중 1개를 확인함" in text
    assert f"확인하지 못한 규칙 1개:\n- `R-2` {inline_code(RULE.text)} · " in text
    assert doc_rules_report.OLD_KIND_TEXT in text and "검사하지 않음" not in text


def test_no_rules_explains_how_to_write_them() -> None:
    for result in (NoRuleDocs(), NoRulesFound(dropped=2)):
        text = _text(result)
        assert f"**규칙:** {NO_RULES}" in text
        assert "`CONTRIBUTING.md`에 팀 규칙을 적어 두면 다음 PR부터" in text
        assert "- ruff 설정이 이미 있는 저장소는 그 설정도 함께 검사합니다." in text
    unread = _text(NoRuleDocs((UnreadDoc("CONTRIBUTING.md", "400KB를 넘음"),)))
    assert "- `CONTRIBUTING.md`: `400KB를 넘음`" in unread


def test_not_run_shows_reason() -> None:
    text = _text(
        NotRun("규칙을 읽지 않음: 호출 상한: 저장소 하루 상한 (500/500회 사용, 이 호출 1회)")
    )
    assert text == (
        "\n**규칙:** 검사하지 못함 "
        "(`규칙을 읽지 않음: 호출 상한: 저장소 하루 상한 (500/500회 사용, 이 호출 1회)`)"
    )


def test_section_stays_within_size_limit() -> None:
    long_rule = replace(RULE, text="규칙 " * 200)
    outcomes = tuple(
        RuleChecked(replace(long_rule, id=f"R-{i}"), replace(REF, key=f"k{i}")) for i in range(80)
    )
    violations = tuple(_violation(line=n, rule=f"R-{n % 80}") for n in range(1, 400))
    checkers = {f"k{i}": replace(CHECKER, body="x" * 9000) for i in range(80)}
    text = _text(_found(*outcomes, violations=violations), checkers)
    assert len(text) <= doc_rules_report.MAX_SECTION_CHARS
    assert doc_rules_report.OMITTED in text
    assert text.rstrip().endswith("</details>")


def test_install_failure_comment_keeps_doc_rules() -> None:
    found = _found(RuleChecked(RULE, REF), violations=(_violation(),))
    text = report.render_install_failure("a" * 40, "only sdist", doc_rules=found,
                                         checkers={"k1": CHECKER})  # fmt: skip
    assert "**테스트:** 실행 불가 (의존성을 설치하지 못함)" in text
    assert "**규칙:** ⚠️ 위반 1건 (규칙 1개)" in text and "```yaml" in text


def test_result_without_doc_rules_has_no_section() -> None:
    text = report.render_result(PytestRun(exit_code=0, summary=None), "a" * 40, 1.0)
    assert "문서 규칙" not in text


def test_file_violation_shows_the_path_without_a_line() -> None:
    rule = replace(RULE, check=CheckMethod("path"))
    ref = replace(REF, kind="path-pattern")
    checker = replace(
        CHECKER, kind="path-pattern", body='{"condition": "must_match", "pattern": "test_"}',
        examples=(Example("tests/pricing_test.py", (1,), "model"),
                  Example("tests/test_a.py", (), "model")),
    )  # fmt: skip
    violation = replace(_violation("tests/pricing_test.py", 0), unit="file")
    text = _text(_found(RuleChecked(rule, ref), violations=(violation,)), {"k1": checker})
    assert "  - `tests/pricing_test.py` (파일 이름·경로)" in text
    assert "pricing_test.py:0" not in text
    assert "```json" in text
    assert "모델이 쓴 예시, 어기는 경로:" in text
    assert "모델이 쓴 예시, 지키는 경로:" in text


def test_dropped_exclusion_is_shown_with_the_rule() -> None:
    rule = replace(RULE, scope=(), exclude_dropped="**/*.py: 적용되는 파일 4개 중 4개를 뺌")
    text = _text(_found(RuleChecked(rule, REF)))
    assert "범위 추정을 버림" in text
    assert "`**/*.py: 적용되는 파일 4개 중 4개를 뺌`" in text


def test_many_violations_show_counts_and_first_ten_per_rule_and_fold_the_rest() -> None:
    other = replace(RULE, id="R-readme-2", text="다른 규칙")
    violations = (
        *(_violation(line=n) for n in range(1, 143)),
        _violation("orders/b.py", 7, rule="R-readme-2"),
    )
    text = _text(_found(RuleChecked(RULE, REF), RuleChecked(other, REF), violations=violations))
    assert "**규칙:** ⚠️ 위반 143건 (규칙 2개)" in text
    assert doc_rules_report.MANY_VIOLATIONS_NOTE in text
    assert "· 142건" in text and "- `R-readme-2` `다른 규칙` · 1건" in text
    head, _, folded = text.partition("<details><summary><code>R-readme-1</code>의 나머지 132건")
    assert "  - `orders/a.py:10`" in head and "  - `orders/a.py:11`" not in head
    assert "  - `orders/a.py:11`" in folded and "  - `orders/a.py:142`" in folded
    assert "  - `orders/b.py:7`" in folded  # 위반이 적은 규칙은 접지 않는다


def test_folded_list_is_capped_and_says_how_many_are_left_out() -> None:
    violations = tuple(_violation(line=n) for n in range(1, 512))
    text = _text(_found(RuleChecked(RULE, REF), violations=violations))
    assert "의 나머지 501건" in text
    assert "  - 외 201건" in text
    assert len(text) <= doc_rules_report.MAX_SECTION_CHARS


def test_fixed_lines_are_listed_and_left_lines_are_summarised_by_reason() -> None:
    """4.9: 고칠 대상은 위치마다 방법이 보이고(테스트 표시는 수정 결과 아래에 묶는다), 고치지 않은
    위반은 규칙마다 "직접 고쳐야 함" 아래에 이유마다 건수와 문장을 한 줄씩 적고 위치를 접는다.
    이유가 여럿이면 위치마다 짧은 이름이 붙는다. "skip"이라는 말은 나오지 않는다."""
    a, b, c, d, e = (_violation(line=n) for n in (2, 3, 4, 5, 6))
    a, d = replace(a, covered=False), replace(d, covered=True)
    decided = Decided((
        FixDecision(a.id, "fix", "model", method="model", why="로거로 바꾼다"),
        FixDecision(b.id, "skip", "model", reason="changes_behavior", why="출력이 기능이다"),
        FixDecision(c.id, "skip", "pipeline", reason="tests_failed", why="테스트가 실패함"),
        FixDecision(d.id, "fix", "pipeline", method="mechanical"),
        FixDecision(e.id, "skip", "model", reason="changes_behavior", why="반환값이 바뀐다"),
    ), calls=1, cached=2, input_tokens=900, output_tokens=70)  # fmt: skip
    found = _found(RuleChecked(RULE, REF), violations=(a, b, c, d, e))
    text = "\n".join(section(found, {}, decided))
    assert "  - `orders/a.py:2` · 고칠 대상 (모델 수정: `로거로 바꾼다`)\n" in text
    assert "  - `orders/a.py:5` · 고칠 대상 (기계적 수정)\n" in text
    assert "실행함" not in text and "실행하지 않음" not in text
    head, _, folded = text.partition("<details><summary>직접 고쳐야 함 3건의 위치</summary>")
    assert ("  직접 고쳐야 함 3건\n"
            "  - 2건: 규칙을 지키는 어떤 변경도 동작을 바꾼다고 모델이 보아 수정안을 만들지 "
            "않았습니다.\n"
            "  - 1건: 테스트가 실패해 고친 것을 확인할 수 없으므로 수정안을 만들지 않았습니다.\n"
            ) in head  # fmt: skip
    assert "orders/a.py:3" not in head and "orders/a.py:4" not in head
    assert "  - `orders/a.py:3` · 고치면 동작이 바뀜: `출력이 기능이다`" in folded
    assert "  - `orders/a.py:4` · 테스트가 실패함" in folded
    assert ("수정 판단: 고칠 대상 2건, 직접 고쳐야 함 3건 (고치면 동작이 바뀜 2건, "
            "테스트가 실패함 1건) · 모델 호출 1회 · 저장된 판단 2건 사용 · "
            "토큰 입력 900, 출력 70") in text  # fmt: skip
    assert "skip" not in text
    assert "수정안:**" not in text  # 묶음의 상태는 따로 넘긴다
    assert "모델 호출 상한에 걸려" not in text


def test_fixed_line_shows_the_fix_pr_that_fixed_it() -> None:
    a, b = _violation(line=2), _violation(line=3)
    decided = Decided(tuple(FixDecision(v.id, "fix", "model", method="model") for v in (a, b)))
    fix = ModelFix("ready", targets=(a.id,), size_capped=(b.id,))
    view = FixView(targets=1, size_capped=1, state="done", fix=fix,
                   proposal=ProposalResult(number=31))  # fmt: skip
    text = "\n".join(section(_found(RuleChecked(RULE, REF), violations=(a, b)), {}, decided,
                             {"model": view}))  # fmt: skip
    assert "  - `orders/a.py:2` · 고칠 대상 (모델 수정) · 수정안 #31\n" in text
    assert "  - `orders/a.py:3` · 고칠 대상 (모델 수정)\n" in text
    # 고친 위반의 표시는 상태와 파일마다 한 줄로 묶는다. 크기 상한으로 빠진 3줄은 넣지 않는다
    assert "테스트:\n- 테스트가 이 줄을 실행하는지 알 수 없음 (1건): `orders/a.py` 줄 2" in text


def test_left_lines_are_folded_even_when_few_and_count_toward_the_fold_cap() -> None:
    violations = tuple(_violation(line=n) for n in range(1, 13))
    decided = Decided(tuple(FixDecision(v.id, "skip", "model", reason="changes_behavior", why="w")
                            for v in violations))  # fmt: skip
    text = "\n".join(section(_found(RuleChecked(RULE, REF), violations=violations), {}, decided))
    assert ("  직접 고쳐야 함 12건 · 규칙을 지키는 어떤 변경도 동작을 바꾼다고 모델이 보아 "
            "수정안을 만들지 않았습니다.") in text  # fmt: skip
    assert "  - `orders/a.py:12` · `w`\n" in text  # 이유가 하나면 까닭은 되풀이하지 않는다
    many = tuple(_violation(line=n) for n in range(1, 512))
    decided = Decided(tuple(FixDecision(v.id, "skip", "pipeline", reason="notify_only",
                                        why="알리기만 하는 규칙") for v in many))  # fmt: skip
    text = "\n".join(section(_found(RuleChecked(RULE, REF), violations=many), {}, decided))
    assert "  - 외 211건" in text and len(text) <= doc_rules_report.MAX_SECTION_CHARS


def _left_text(*pairs: tuple[Violation, FixDecision], rule: Rule = RULE) -> str:
    """위반과 그 수정 판단으로 그린 규칙 절."""
    violations = tuple(v for v, _ in pairs)
    decided = Decided(tuple(d for _, d in pairs))
    return "\n".join(section(_found(RuleChecked(rule, REF), violations=violations), {}, decided))


def _skip(v: Violation, reason: str, why: str, by: str = "pipeline") -> FixDecision:
    return FixDecision(v.id, "skip", by, reason=reason, why=why)  # type: ignore[arg-type]


FILE_SENTENCE: Final = "파일 이름이나 위치를 바꿔야 하는 위반이라 수정안을 만들지 않았습니다."


def test_each_reason_has_one_sentence_per_rule() -> None:
    """수정안을 만들지 않은 까닭은 규칙마다 한 번, "직접 고쳐야 함 N건" 옆에 문장으로 적는다. 위치
    줄에는 되풀이하지 않는다. "알리기만 하는 규칙"이라는 말은 나오지 않는다 (2026-11-23)."""
    v = _violation()
    cases = [
        (_skip(v, "notify_only", "알리기만 하는 규칙"),
         "규칙을 읽은 모델이 그 파일 안에서는 고칠 수 없는 규칙(다른 파일의 사용처까지 바꿔야 "
         "하거나 코드로 고칠 수 없음)이라고 답해 수정안을 만들지 않았습니다."),
        (_skip(v, "notify_only", "도구의 자동 수정이 없음"),
         "도구에 이 위반의 자동 수정이 없어 수정안을 만들지 않았습니다."),
        (_skip(v, "tests_failed", "테스트가 시간 안에 끝나지 않음"),
         "테스트가 시간 안에 끝나지 않아 고친 것을 확인할 수 없으므로 수정안을 만들지 않았습니다."),
        (_skip(v, "tests_failed", "테스트를 돌리지 못함"),
         "테스트를 돌리지 못해 고친 것을 확인할 수 없으므로 수정안을 만들지 않았습니다."),
        (_skip(v, "no_safety_net", "테스트로 확인할 수 없음 (테스트가 없음)"),
         "테스트가 없어 고친 것을 확인할 수 없으므로 수정안을 만들지 않았습니다."),
        (_skip(v, "no_safety_net", "테스트로 확인할 수 없음 (불안정한 테스트가 있음)"),
         "불안정한 테스트가 있어 고친 것을 확인할 수 없으므로 수정안을 만들지 않았습니다."),
        (_skip(v, "call_limit", "수정 판단을 하지 못함 (호출 상한: PR 상한 (3/3회 사용))"),
         "모델 호출 상한에 걸려 고칠지 판단하지 못했습니다. `/fix`로 다시 돌릴 수 있습니다."),
        (_skip(v, "undecided", "수정 판단을 하지 못함 (모델 호출 실패)"),
         "모델 호출이 실패해 고칠지 판단하지 못했습니다."),
        (_skip(v, "undecided", "모델이 답하지 않음"),
         "모델이 이 위반에 답하지 않아 수정안을 만들지 않았습니다."),
        (_skip(v, "undecided", ""),
         "모델이 고칠지 판단하지 못해 수정안을 만들지 않았습니다."),
        (_skip(v, "needs_other_file", "`b.py`의 호출부도 바꿔야 함", by="model"),
         "고치려면 그 파일 밖의 다른 파일이나 다른 파일이 쓰는 이름을 바꿔야 한다고 모델이 보아 "
         "수정안을 만들지 않았습니다."),
        (_skip(v, "team_pattern", "기록 12와 같음", by="model"),
         "팀이 전에 닫은 수정안과 비슷하다고 모델이 보아 수정안을 만들지 않았습니다."),
        (_skip(v, "risky", "잘 모르겠음", by="model"),
         "모델이 고치지 않기로 해 수정안을 만들지 않았습니다."),
    ]  # fmt: skip
    for decision, sentence in cases:
        text = _left_text((v, decision))
        assert f"  직접 고쳐야 함 1건 · {sentence}\n" in text, decision.reason
        assert "알리기만 하는 규칙" not in text
        located = text.partition("직접 고쳐야 함 1건의 위치</summary>")[2]
        assert sentence not in located
        assert "수정 판단: 고칠 대상 0건, 직접 고쳐야 함 1건 ·" in text  # 이유가 하나면 괄호 없음


def test_location_keeps_only_what_differs_per_line() -> None:
    """위치 줄에는 위치마다 다른 것만 붙는다: 모델이 쓴 이유 글(코드 스팬), 거절된 수정안 번호."""
    a, b = _violation(line=1), _violation(line=2)
    text = _left_text((a, _skip(a, "notify_only", "알리기만 하는 규칙")),
                      (b, _skip(b, "notify_only", "알리기만 하는 규칙")))  # fmt: skip
    assert "  - `orders/a.py:1`\n" in text and "  - `orders/a.py:2`\n" in text
    rejected = _left_text((a, _skip(a, "rejected_before", "이전에 거절됨 (#900)")))
    assert ("  직접 고쳐야 함 1건 · 같은 줄을 고친 수정안이 머지되지 않고 닫혀 다시 만들지 "
            "않았습니다.") in rejected  # fmt: skip
    assert "  - `orders/a.py:1` · 이전에 거절됨 (#900)" in rejected


def test_mixed_reasons_are_split_with_counts_and_named_per_line() -> None:
    """한 규칙에 이유가 여럿이면 이유마다 건수와 문장을 한 줄씩 적고, 위치마다 짧은 이름을 붙인다.
    "수정 판단:" 줄에는 이유별 건수가 붙는다."""
    a, b, c = _violation(line=1), _violation(line=2), replace(_violation("x.py", 0), unit="file")
    text = _left_text(
        (a, _skip(a, "notify_only", "도구의 자동 수정이 없음")),
        (b, _skip(b, "changes_behavior", "반환값이 바뀐다", by="model")),
        (c, _skip(c, "notify_only", "알리기만 하는 규칙")),
    )
    assert ("  직접 고쳐야 함 3건\n"
            "  - 1건: 도구에 이 위반의 자동 수정이 없어 수정안을 만들지 않았습니다.\n"
            "  - 1건: 규칙을 지키는 어떤 변경도 동작을 바꾼다고 모델이 보아 수정안을 만들지 "
            "않았습니다.\n"
            f"  - 1건: {FILE_SENTENCE}\n") in text  # fmt: skip
    assert "  - `orders/a.py:1` · 도구의 자동 수정이 없음\n" in text
    assert "  - `orders/a.py:2` · 고치면 동작이 바뀜: `반환값이 바뀐다`\n" in text
    assert "  - `x.py` (파일 이름·경로) · 파일 이름·위치를 바꿔야 함\n" in text
    assert ("수정 판단: 고칠 대상 0건, 직접 고쳐야 함 3건 (도구의 자동 수정이 없음 1건, "
            "고치면 동작이 바뀜 1건, 파일 이름·위치를 바꿔야 함 1건) ·") in text  # fmt: skip


def test_unknown_reason_from_an_old_checkpoint_shows_the_pipeline_reason() -> None:
    v = _violation()
    text = _left_text((v, _skip(v, "not_covered", "테스트가 지나가지 않음")))
    assert "  직접 고쳐야 함 1건 · 수정안을 만들지 않았습니다 (테스트가 지나가지 않음)." in text


def test_problem_is_lifted_once_when_violations_share_a_message() -> None:
    """같은 메시지를 가진 위반들의 "무엇이 문제인가"는 규칙 블록에 한 줄로 오른다. 메시지는 코드
    스팬 안에 길이 상한을 두고 넣는다."""
    message = "`print` 대신 `logger`를 쓴다 @team"
    vs = tuple(replace(_violation(line=n), message=message) for n in (1, 2))
    text = _text(_found(RuleChecked(RULE, REF), violations=vs))
    assert text.count("무엇이 문제인가") == 1
    assert "  출처: `README.md > 코드 규칙 > 출력` · 모델이 쓴 검사기 (ast-grep)\n" \
           "  무엇이 문제인가: `` `print` 대신 `logger`를 쓴다 @team ``\n" in text  # fmt: skip
    assert "@" not in _outside_code(text)
    long = (replace(_violation(), message="x" * 1000),)
    shown = _text(_found(RuleChecked(RULE, REF), violations=long))
    assert f"무엇이 문제인가: `{'x' * doc_rules_report.MAX_DETAIL_CHARS}…`" in shown


def test_problem_is_left_out_when_it_says_nothing_new() -> None:
    """메시지가 서로 다르거나, 없거나, 규칙 원문과 공백·대소문자·마침표만 다르거나, 위치 줄에 이미
    보이면(도구 설정의 규칙) 싣지 않는다."""
    rule = replace(RULE, text="New files are written in TypeScript.")
    differ = (replace(_violation(line=1), message="a"), replace(_violation(line=2), message="b"))
    empty = (replace(_violation(), message=" "),)
    same = (replace(_violation(), message="new  files are written in typescript"),)
    for vs in (differ, empty, same):
        assert "무엇이 문제인가" not in _text(_found(RuleChecked(rule, REF), violations=vs))
    tool_rule = replace(RULE, id="E741", origin="tool_config")
    tool = (Violation(id="t", rule_id="E741", file="a.py", line=3, code="E741",
                      message="Ambiguous variable name: `l`", found_by="tool"),)  # fmt: skip
    text = _text(_found(RuleChecked(tool_rule, REF), violations=tool))
    assert "무엇이 문제인가" not in text
    assert "  - `a.py:3` `` Ambiguous variable name: `l` ``" in text


TS_RULE: Final = Rule(
    id="R-readme-9b6db85d",
    text="New files and contracts are written in TypeScript or .d.ts (S1).",
    source=RuleSource("README.md", "Tech stack", "abc"), origin="document", scope=(),
    check=CheckMethod("path"), fix="none",
)  # fmt: skip
TS_MESSAGE: Final = (
    "New files and contracts must be written in TypeScript or .d.ts, not JavaScript."
)
TS_FILES: Final = (
    "src/map-edit.cjs", "src/room-state.cjs", "src/net/socket.cjs", "src/net/lobby.cjs",
    "src/ui/panel.js", "src/ui/toolbar.js", "src/ui/dialog.js", "src/ui/menu.js",
    "server/index.cjs", "server/routes.cjs", "server/store.cjs", "scripts/build.mjs",
)  # fmt: skip


def ts_comment() -> str:
    """주인이 받은 코멘트와 같은 입력(JavaScript 저장소, 파일 단위 위반 12건, 테스트 없음, 수정안
    없음)으로 그린 결과 코멘트. 파일 이름은 예시다."""
    ref = CheckerRef(key="k", id="d" * 64, kind="path-pattern", cached=False)
    violations = tuple(
        Violation(id=f"v{i}", rule_id=TS_RULE.id, file=f, line=0, message=TS_MESSAGE,
                  found_by="checker", unit="file")
        for i, f in enumerate(TS_FILES)
    )  # fmt: skip
    found = _found(RuleChecked(TS_RULE, ref), violations=violations)
    decided = Decided(tuple(_skip(v, "notify_only", "알리기만 하는 규칙") for v in violations))
    run = PytestRun(exit_code=5, summary=ReportSummary())
    return report.render_result(run, "a" * 40, 12.0, doc_rules=found, decisions=decided)


def test_the_reported_typescript_comment_explains_what_and_why() -> None:
    """pr-proof#78: 무엇이 문제인지, 왜 수정안을 만들지 않았는지, 테스트가 왜 돌지 않았는지가 위반
    가까이에 있다."""
    text = ts_comment()
    head, _, folded = text.partition(doc_rules_report.DETAILS_SUMMARY)
    assert "**테스트:** 실행된 테스트 없음\n**규칙:** ⚠️ 위반 12건 (규칙 1개)\n" in text
    assert report.untested_note(PytestRun(exit_code=5, summary=ReportSummary()), True)[1] in head
    assert (
        "- `R-readme-9b6db85d` `New files and contracts are written in TypeScript or .d.ts (S1).` "
        "· 12건\n"
        "  출처: `README.md > Tech stack` · 모델이 쓴 검사기 (path-pattern)\n"
        f"  무엇이 문제인가: `{TS_MESSAGE}`\n"
        f"  직접 고쳐야 함 12건 · {FILE_SENTENCE}\n"
    ) in head
    assert "  - `src/map-edit.cjs` (파일 이름·경로)\n" in head
    assert "알리기만 하는 규칙" not in text and "검색:" not in text
    assert "수정 판단: 고칠 대상 0건, 직접 고쳐야 함 12건 · 모델 호출 0회" in head
    assert "읽은 규칙 1개" in folded


def test_model_reason_cannot_escape_code() -> None:
    nasty = "@team <img src=x onerror=alert(1)> ``` [x](http://e) **b**\n# 제목"
    v = _violation()
    for decision in (
        FixDecision(v.id, "skip", "model", reason="risky", why=nasty),
        FixDecision(v.id, "fix", "model", method="model", why=nasty),
    ):
        text = "\n".join(section(_found(RuleChecked(RULE, REF), violations=(v,)), {},
                                  Decided((decision,))))  # fmt: skip
        outside = _outside_code(text)
        assert "@" not in outside and "<img" not in outside and "](http" not in outside
        assert "\n# 제목" not in text


def test_cached_decisions_show_no_token_counts() -> None:
    """9.4: 토큰 수는 이번에 실제로 부른 호출만 센다. 저장된 판단만 썼으면 토큰 줄이 없다."""
    v = _violation()
    decided = Decided((FixDecision(v.id, "skip", "model", reason="risky", why="w"),), cached=1)
    text = "\n".join(section(_found(RuleChecked(RULE, REF), violations=(v,)), {}, decided))
    assert "· 모델 호출 0회 · 저장된 판단 1건 사용\n" in text + "\n" and "토큰 입력 0" not in text


def test_without_decisions_the_section_is_unchanged() -> None:
    found = _found(RuleChecked(RULE, REF), violations=(_violation(),))
    assert section(found, {}) == section(found, {}, None)
    assert "수정 판단" not in _text(found)


READY: Final = ModelFix(
    status="ready", targets=("v1", "v2"), explanation=None,
    verification=Verification(tests=9, resolved=2, rules_rechecked=1, files=1, lines=4),
    changed_files=("orders/a.py",), calls=1, input_tokens=1200, output_tokens=80,
    models=("claude-haiku-5-5",), attempts=1,
)  # fmt: skip


def _fix_text(view: FixView, bundle: str = "model") -> str:
    found = _found(RuleChecked(RULE, REF), violations=(_violation(),))
    return "\n".join(
        [title(found, {bundle: view}) or "", *section(found, {}, None, {bundle: view})]
    )


def test_fix_result_names_the_pull_request_and_the_verification() -> None:
    """수정안 PR 번호와 파이프라인이 확인한 검증 결과가 보인다 (4.9). 아직 안 만든다는 글은 없다."""
    text = _fix_text(FixView(2, 0, "done", fix=READY, proposal=ProposalResult(number=901)))
    assert ("**모델 수정안:** 위반 2건을 고쳤습니다. 검증: 테스트 9개가 수정 전과 같은 결과로 "
            "통과, 대상 위반 2건 해소, 새 위반 없음. 수정안 PR #901을 열었습니다."
            ) in text  # fmt: skip
    assert text.startswith("**규칙:** ⚠️ 위반 1건 (규칙 1개) · 수정안 #901")
    assert ("수정 작성: 모델 호출 1회, 회차 1 · `claude-haiku-5-5` · 저장된 결과 0건 사용 · "
            "토큰 입력 1200, 출력 80") in text  # fmt: skip
    assert "아직 만들지 않습니다" not in text


def test_fix_states_before_and_without_a_fix(tmp_path) -> None:
    assert "수정안을 만드는 중" in _fix_text(FixView(3, 0, "running"))
    approval = _fix_text(FixView(3, 0, "approval"))
    assert "`require_approval: true`" in approval and "`/fix model`을 달면 만듭니다." in approval
    assert "모델 수정안 PR #905이 열려 있습니다" in _fix_text(FixView(3, 0, "open", number=905))
    capped = _fix_text(FixView(20, 4, "running"))
    assert "수정안 크기 상한으로 처리하지 못함: 4건" in capped
    failed = replace(READY, status="failed", verification=None, detail="고친 뒤 테스트: 실패")
    text = _fix_text(FixView(2, 0, "done", fix=failed))
    assert (
        "**모델 수정안:** 수정안을 만들지 못함: 정해진 회차 안에 검증을 통과하지 못함 (`고친 뒤"
        in text
    )
    capped_calls = replace(READY, status="budget", detail="호출 상한: PR 상한 (3/3회 사용)")
    capped_text = _fix_text(FixView(2, 0, "done", fix=capped_calls))
    assert "모델 호출 상한에 걸려 처리하지 못함: 수정 2건" in capped_text
    assert "`/fix`로 다시 돌릴 수 있습니다." in capped_text


def test_fork_shows_the_fix_in_the_comment_instead_of_a_pull_request() -> None:
    patch = "--- a/orders/a.py\n+++ b/orders/a.py\n@@ -2 +2 @@\n-print(x)\n+log(x)\n"
    fork = ProposalResult(
        reason="포크에서 온 PR이라 수정안 PR을 열 수 없음 (수정 내용은 결과 코멘트에)"
    )
    text = _fix_text(FixView(2, 0, "done", fix=READY, proposal=fork, patch=patch))
    assert "수정안 PR을 만들지 않았습니다 (포크에서 온 PR" in text
    assert "<details><summary>수정 내용</summary>" in text and "+log(x)" in text
    secret = ProposalResult(reason="비밀값으로 보이는 내용이 있음 (토큰·키 형식)")
    assert "+log(x)" not in _fix_text(
        FixView(2, 0, "done", fix=READY, proposal=secret, patch=patch)
    )


MECHANICAL: Final = MechanicalFix(
    status="ready", targets=("v1", "v2"), fixed=("v1",), remaining=("v2",),
    changed_files=("orders/a.py",), formatted_files=("orders/a.py",), tests=9,
    test_line="통과 (9개)", attempt_id=3,
)  # fmt: skip


def test_mechanical_fix_result_names_the_pull_request_format_and_what_is_left() -> None:
    text = _fix_text(
        FixView(2, 0, "done", fix=MECHANICAL, proposal=ProposalResult(number=902)), "mechanical"
    )
    assert text.startswith("**규칙:** ⚠️ 위반 1건 (규칙 1개) · 수정안 #902")
    assert (
        "**기계적 수정안:** 위반 1건을 고쳤습니다 (도구의 안전한 수정). 검증: 고친 뒤 테스트 "
        "통과 (9개) (수정 전과 같은 결과), 새 위반 없음. 수정안 PR #902을 열었습니다."
    ) in text
    assert "`ruff format`을 고친 파일 1개에 적용했습니다." in text
    assert "자동으로 고치지 못한 위반 1건은 그대로 남습니다." in text
    assert "수정 작성: 모델 호출" not in text  # 기계적 수정은 모델을 부르지 않는다


def test_mechanical_fix_failures_and_waiting_states() -> None:
    dropped = replace(MECHANICAL, status="tests_changed", detail="실패 (9개 중 1개)",
                      format_skipped="base 브랜치가 ruff format을 따르지 않음")  # fmt: skip
    text = _fix_text(FixView(2, 0, "done", fix=dropped), "mechanical")
    assert ("**기계적 수정안:** 수정안을 버림: 고친 뒤 테스트 결과가 달라짐 (`실패 (9개 중 1개)`)"
            ) in text  # fmt: skip
    approval = _fix_text(FixView(2, 0, "approval"), "mechanical")
    assert "`/fix mechanical`을 달면 만듭니다." in approval
    merged = _fix_text(FixView(2, 0, "merged", number=903), "mechanical")
    assert "기계적 수정안 PR #903을 머지한 결과라" in merged


def test_mechanical_fix_without_a_pull_request_shows_the_patch_unless_it_has_a_secret() -> None:
    patch = '-"""\n-```\n+x\n'
    fork = ProposalResult(reason="포크에서 온 PR이라 수정안 PR을 열 수 없음")
    text = _fix_text(FixView(2, 0, "done", fix=MECHANICAL, proposal=fork, patch=patch),
                     "mechanical")  # fmt: skip
    assert "````diff" in text and "</details>" in text  # 패치의 백틱이 울타리를 닫지 못한다
    secret = ProposalResult(reason="비밀값으로 보이는 내용이 있음 (토큰·키 형식)")
    hidden = _fix_text(FixView(2, 0, "done", fix=MECHANICAL, proposal=secret, patch=patch),
                       "mechanical")  # fmt: skip
    assert "수정 내용" not in hidden


def test_judged_rule_shows_why_it_went_to_model_judgment() -> None:
    """모델 판단으로 보낸 까닭마다 다른 글. none·part는 모델이 적은 부분을 붙인다 (4.4)."""
    cases = {
        "none": "모델 판단 (검사기로 가릴 수 없는 규칙이라고 검사기 작성 모델이 답함: `주석의 뜻`)",
        "part": "모델 판단 (검사기로는 일부만 가릴 수 있다고 검사기 작성 모델이 답함: `주석의 뜻`)",
        "examples": "모델 판단 (검사기가 예시 확인을 두 번 통과하지 못함)",
        "attempts": "모델 판단 (예시를 통과하는 검사기를 만들지 못함)",
    }
    for route, shown in cases.items():
        text = _text(_found(RuleJudged(RULE, "주석의 뜻", route)))
        assert f"  출처: `README.md > 코드 규칙 > 출력` · 범위: `orders/**` · 확인: {shown}" in text
    # 실패 이유(예시 확인의 기대와 실제)는 확인 방법 칸에 넣지 않는다
    assert "예시 2개" not in _text(_found(RuleJudged(RULE, "예시 2개를 틀리게 가림", "examples")))
    bare = _text(_found(RuleJudged(RULE, "", "none")))
    assert "모델 판단 (검사기로 가릴 수 없는 규칙이라고 검사기 작성 모델이 답함)" in bare


def test_rule_checked_by_a_tool_names_the_tool_and_codes() -> None:
    """문서 규칙을 ruff 코드로 확인하면 출처는 문서이고 확인 방법은 도구와 코드다 (3-2)."""
    ref = CheckerRef(key="k2", id="d" * 64, kind="ruff", cached=False, codes=("F401",))
    v = replace(_violation(), found_by="tool", code="F401", message="`os` imported but unused")
    text = _text(_found(RuleChecked(RULE, ref), violations=(v,)), {})
    assert ("  출처: `README.md > 코드 규칙 > 출력` · 도구 `ruff` `F401` (모델이 도구 목록에서 "
            "고르고 예시로 확인함)") in text  # fmt: skip
    assert "모델이 쓴 검사기" not in text  # 도구는 모델이 쓴 검사기가 아니다


def test_rule_documents_that_could_not_be_read_are_named_when_tool_rules_ran() -> None:
    found = replace(_found(RuleChecked(RULE, REF)), note="규칙 읽기의 모델 호출이 실패함: x")
    assert (
        "규칙 문서를 읽지 못해 도구 설정의 규칙만 검사함 (`규칙 읽기의 모델 호출이 실패함: x`)"
        in (_text(found))
    )


def test_what_the_model_says_is_not_a_violation_is_set_aside_not_counted() -> None:
    """도구가 잡았어도 수정 판단의 모델이 위반이 아니라고 답한 것은 위반 수에도 "직접 고쳐야 함"에도
    들지 않는다. 사람이 뒤집어 볼 수 있게 접는 블록에 위치와 모델의 까닭이 남는다."""
    real, false = _violation(line=2), _violation(line=3)
    decided = Decided((
        FixDecision(real.id, "skip", "model", reason="changes_behavior", why="동작"),
        FixDecision(false.id, "skip", "model", reason="not_a_violation", why="<문서의 예외>"),
    ))  # fmt: skip
    found = _found(RuleChecked(RULE, REF), violations=(real, false))
    assert title(found, decisions=decided) == "**규칙:** ⚠️ 위반 1건 (규칙 1개)"
    text = "\n".join(section(found, {}, decided))
    shown, _, folded = text.partition(doc_rules_report.DETAILS_SUMMARY)
    assert "`orders/a.py:3`" not in shown
    assert "  직접 고쳐야 함 1건 · 규칙을 지키는 어떤 변경도 동작을 바꾼다고" in shown
    assert "모델이 위반이 아니라고 판단함 1건" in shown
    assert ("도구가 잡았지만 모델이 위반이 아니라고 판단함 1건:\n"
            "- `R-readme-1` `orders/a.py:3` · `<문서의 예외>`") in folded  # fmt: skip
    only_false = Decided((FixDecision(false.id, "skip", "model", reason="not_a_violation"),))
    clean = _found(RuleChecked(RULE, REF), violations=(false,))
    assert title(clean, decisions=only_false) == (
        "**규칙:** ✅ 위반 없음 · 규칙 1개를 확인함 (도구·검사기 1개)"
    )


def test_rule_the_model_could_not_judge_is_listed_with_its_reason() -> None:
    """모델이 코드를 보고 판단하지 못한 규칙은 확인한 것으로 세지 않고, 까닭과 함께 펼쳐 보인다."""
    judged = RuleJudged(replace(RULE, id="R-2"), "", "none", pairs=3, undecided=3,
                        undecided_why="hunk 밖의 데이터를 봐야 함")  # fmt: skip
    partly = RuleJudged(replace(RULE, id="R-3"), "", "none", pairs=4, undecided=1,
                        undecided_why="x")  # fmt: skip
    done = RuleJudged(replace(RULE, id="R-4"), "", "none", pairs=2)
    text = _text(_found(RuleChecked(RULE, REF), judged, partly, done))
    assert (
        "**규칙:** ✅ 위반 없음 · 규칙 4개 중 2개를 확인함 (도구·검사기 1개, 모델 판단 1개)" in text
    )
    shown = text.partition("<details>")[0]
    assert "확인하지 못한 규칙 2개:" in shown
    assert (
        "- `R-2` " in shown
        and "모델이 코드를 보고 판단하지 못함: `hunk 밖의 데이터를 봐야 함`" in shown
    )
    assert "모델이 코드를 보고 판단하지 못함 (바뀐 곳 4군데 중 1군데): `x`" in shown


def test_run_rule_shows_the_models_answer_and_the_results_it_saw() -> None:
    """실행 결과의 규칙은 모델이 테스트·커버리지·정적 검사를 보고 정한 답으로 보인다."""
    run_rule = replace(RULE, id="R-run", target="run", runs=("tests",))
    facts = "테스트 통과 (6개) · 있던 테스트가 PR이 바꾼 줄 220줄 중 0줄을 실행함 · 정적 검사 x"
    outcome = RuleUnchecked(run_rule, "run", "")
    broken = replace(
        _found(RuleChecked(RULE, REF), outcome),
        runs=(RunJudged("R-run", "broken", "새 생성기를 실행하는 테스트가 없음", facts),),
    )
    text = _text(broken)
    assert "**규칙:** ⚠️ 위반 1건 (규칙 1개)" in text
    assert "테스트·정적 검사 결과로 어긴 규칙 (줄이 아니라 PR에 걸려 수정안은 만들지 않음):" in text
    assert f"  근거: {facts}" in text
    kept = replace(broken, runs=(RunJudged("R-run", "kept", "실행함", facts),))
    assert "규칙 2개를 확인함 (도구·검사기 1개, 테스트·정적 검사 결과 1개)" in _text(kept)
    assert "확인하지 못한 규칙" not in _text(kept)
    before_tests = _text(_found(RuleChecked(RULE, REF), outcome))
    assert "규칙 2개 중 1개를 확인함" in before_tests
    assert (
        "모델이 테스트·정적 검사 결과로 판단하지 못함: `테스트를 돌리기 전에 끝남`" in before_tests
    )


def test_reasked_pairs_with_a_prior_answer_get_one_folded_line() -> None:
    """읽은 곳이 바뀌어 지난 판단을 주고 다시 물은 쌍이 있을 때만 접는 블록에 한 줄 더한다."""
    judged = RuleJudged(RULE, "뜻을 읽어야 한다", "none", pairs=3)
    usage = JudgeUsage(rules=1, hunks=3, calls=1, cached=1, dropped=0, reasked=2, kept_prior=1)
    found = replace(_found(judged), judge=usage)
    text = _text(found)
    details = text.split("<details>")[1]
    assert "모델 판단: 지난 판단을 주고 다시 물음 2쌍, 그중 유지 1쌍" in details
    plain = _text(replace(found, judge=replace(usage, reasked=0, kept_prior=0)))
    assert "지난 판단을 주고" not in plain and "모델 판단: 규칙 1개" in plain
