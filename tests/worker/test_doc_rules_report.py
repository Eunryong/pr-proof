"""worker/doc_rules_report.py: 결과 코멘트의 "문서 규칙" 절. 믿지 않는 글의 처리와 길이 상한."""

import re
from dataclasses import replace
from typing import Final

from app.models.checker import Checker
from app.models.doc_rules import (
    CallUsage,
    CheckerRef,
    Found,
    NoRuleDocs,
    NoRulesFound,
    NotRun,
    RuleChecked,
    RuleUnchecked,
)
from app.models.fix_decision import Decided, FixDecision
from app.models.model_fix import ModelFix, Verification
from app.models.proposal import ProposalResult
from app.models.rule import CheckMethod, Example, Rule, RuleSource, UnreadDoc
from app.models.test_run import PytestRun
from app.models.violation import Violation
from app.worker import doc_rules_report, report
from app.worker.doc_rules_report import HOW_TO_CORRECT, NO_RULES, FixView, section
from app.worker.markdown import inline_code

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
    return "\n".join(section(result, checkers or {}))


def _outside_code(text: str) -> str:
    """코드 울타리와 코드 스팬을 뺀 글. 마크다운·HTML·멘션이 동작하는 부분이다."""
    text = re.sub(r"(?ms)^(`{3,})[^\n]*\n.*?^\1$", "", text)
    return re.sub(r"(`+)(?!`).*?(?<!`)\1(?!`)", "", text)


def test_violation_lists_location_quote_source_and_checker() -> None:
    text = _text(_found(RuleChecked(RULE, REF), violations=(_violation(),)), {"k1": CHECKER})
    assert "**문서 규칙:** ⚠️ 위반 1건 (규칙 1개)" in text
    assert "- `R-readme-1` `` `orders/` 아래 모듈은 `print`를 쓰지 않는다. `` · 1건" in text
    assert "  - `orders/a.py:2`" in text
    assert doc_rules_report.MANY_VIOLATIONS_NOTE not in text
    assert "  출처: `README.md > 코드 규칙 > 출력` · 검사기 `cccccccc` (ast-grep)" in text
    assert "```yaml\nid: no-print\nlanguage: python\nrule:\n  pattern: print($$$A)\n```" in text
    assert "예시: 문서 1개, 모델이 쓴 것 1개" in text
    assert "모델이 쓴 예시, 정상:\n\n```python\nlog(1)\n```" in text
    assert "print(1)" not in text  # 문서의 예시는 문서에 있으므로 다시 싣지 않는다
    assert text.rstrip().endswith(HOW_TO_CORRECT)
    assert "모델 호출 3회 / 상한 14회 (규칙 읽기와 검사기 작성) · `claude-haiku-5-5` · " \
           "저장된 결과 1건 사용 · 토큰 입력 3000, 출력 300" in text  # fmt: skip


def test_checker_without_violation_has_no_body() -> None:
    text = _text(_found(RuleChecked(RULE, REF)), {"k1": CHECKER})
    assert "**문서 규칙:** ✅ 위반 없음 (규칙 1개 검사)" in text
    assert "모델이 쓴 검사기" not in text and "읽은 규칙 1개" in text


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
        RuleUnchecked(replace(RULE, id="R-2", target="human"), "human", "사람이 판단할 규칙"),
        RuleUnchecked(replace(RULE, id="R-6", target="pr"), "pr", "커밋 메시지의 규칙"),
        RuleUnchecked(replace(RULE, id="R-3"), "cap", "호출 상한: 규칙 묶음 상한 (3/3회 사용)"),
        RuleUnchecked(replace(RULE, id="R-4"), "cap", "호출 상한: 규칙 묶음 상한 (3/3회 사용)"),
        RuleUnchecked(replace(RULE, id="R-5"), "tool_unavailable", "도구를 준비하지 못함: x"),
    )
    text = _text(_found(*outcomes, disabled=2))
    assert (
        "**문서 규칙:** 검사한 규칙 없음 (읽은 규칙 6개) · PR 단위 규칙 1개 · "
        "사람이 확인하는 규칙 1개 · `rules.disabled`로 끈 규칙 2개" in text
    )
    assert (
        "읽었지만 아직 검사하지 못하는 규칙 1개 (예시를 통과하는 검사기가 없음):\n- `R-1`" in text
    )
    assert "이번 검사에서 검사하지 못한 규칙 1개 (검사하지 못함 (도구 준비 실패) 1개):" in text
    assert ("모델 호출 상한에 걸려 처리하지 못함: 검사기 작성 2개 규칙 "
            "(`호출 상한: 규칙 묶음 상한 (3/3회 사용)`). 다음 검사") in text  # fmt: skip
    # 코드 줄로 확인하지 않는 규칙은 검사하지 못한 규칙과 따로 목록으로 보인다
    assert ("PR 단위로 확인할 규칙 1개 (커밋 메시지, PR 본문, 브랜치, 바뀐 파일 종류. "
            "아직 확인하지 않음):\n- `R-6`") in text  # fmt: skip
    assert "사람이 확인하는 규칙 1개 (파이프라인이 검사하지 않음):\n- `R-2`" in text
    assert "확인: 사람이 확인하는 규칙 (검사하지 않음)" in text


def test_title_counts_rules_outside_code_apart_from_unchecked() -> None:
    outcomes = (
        RuleChecked(RULE, REF),
        RuleUnchecked(replace(RULE, id="R-1"), "no_checker", "3회 안에 예시를 통과하지 못함"),
        RuleUnchecked(replace(RULE, id="R-2", target="human"), "human", "절차"),
        RuleUnchecked(replace(RULE, id="R-3", target="human"), "human", "절차"),
    )
    assert (
        "**문서 규칙:** ✅ 위반 없음 (규칙 1개 검사) · 검사하지 못한 규칙 1개 · "
        "사람이 확인하는 규칙 2개"
    ) in _text(_found(*outcomes))


def test_no_rules_explains_how_to_write_them() -> None:
    for result in (NoRuleDocs(), NoRulesFound(dropped=2)):
        text = _text(result)
        assert f"**문서 규칙:** {NO_RULES}" in text
        assert "`## 코드 규칙` 같은 절을 두고 규칙을 한 문장씩 적으면" in text
    unread = _text(NoRuleDocs((UnreadDoc("CONTRIBUTING.md", "400KB를 넘음"),)))
    assert "- `CONTRIBUTING.md`: `400KB를 넘음`" in unread


def test_not_run_shows_reason() -> None:
    text = _text(
        NotRun("규칙을 읽지 않음: 호출 상한: 저장소 하루 상한 (500/500회 사용, 이 호출 1회)")
    )
    assert text == (
        "\n**문서 규칙:** 검사하지 못함 "
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
    assert text.rstrip().endswith(HOW_TO_CORRECT)


def test_install_failure_comment_keeps_doc_rules() -> None:
    found = _found(RuleChecked(RULE, REF), violations=(_violation(),))
    text = report.render_install_failure("a" * 40, "only sdist", doc_rules=found,
                                         checkers={"k1": CHECKER})  # fmt: skip
    assert "**테스트:** 실행 불가 (의존성을 설치하지 못함)" in text
    assert "**문서 규칙:** ⚠️ 위반 1건 (규칙 1개)" in text and "```yaml" in text


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
    assert "**문서 규칙:** ⚠️ 위반 143건 (규칙 2개)" in text
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


def test_each_violation_shows_its_decision_and_the_counts() -> None:
    a, b, c, d = (_violation(line=n) for n in (2, 3, 4, 5))
    decided = Decided((
        FixDecision(a.id, "fix", "model", method="model", why="로거로 바꾼다"),
        FixDecision(b.id, "skip", "model", reason="risky", why="출력이 기능이다"),
        FixDecision(c.id, "skip", "pipeline", reason="not_covered", why="테스트가 지나가지 않음"),
        FixDecision(d.id, "fix", "pipeline", method="mechanical"),
    ), calls=1, cached=2)  # fmt: skip
    text = "\n".join(section(_found(RuleChecked(RULE, REF), violations=(a, b, c, d)), {}, decided))
    assert "  - `orders/a.py:2` · fix (모델 수정: `로거로 바꾼다`)" in text
    assert "  - `orders/a.py:3` · skip (모델: `출력이 기능이다`)" in text
    assert "  - `orders/a.py:4` · skip (테스트가 지나가지 않음)" in text
    assert "  - `orders/a.py:5` · fix (기계적 수정)" in text
    assert ("수정 판단: fix 2건, skip 2건 (모델이 고치지 않기로 함 1건, 테스트가 지나가지 않음 1건)"
            " · 모델 호출 1회 · 저장된 판단 2건 사용") in text  # fmt: skip
    assert "**수정안:**" not in text  # 모델 수정 묶음의 상태는 따로 넘긴다
    assert "모델 호출 상한에 걸려" not in text


def test_folded_violations_carry_their_decision_too() -> None:
    violations = tuple(_violation(line=n) for n in range(1, 13))
    decided = Decided(tuple(FixDecision(v.id, "skip", "model", reason="risky", why="w")
                            for v in violations))  # fmt: skip
    text = "\n".join(section(_found(RuleChecked(RULE, REF), violations=violations), {}, decided))
    assert "  - `orders/a.py:12` · skip (모델: `w`)" in text


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


def test_without_decisions_the_section_is_unchanged() -> None:
    found = _found(RuleChecked(RULE, REF), violations=(_violation(),))
    assert section(found, {}) == section(found, {}, None)
    assert "수정 판단" not in _text(found)


READY: Final = ModelFix(
    status="ready", targets=("v1", "v2"), explanation=None,
    verification=Verification(tests=9, resolved=2, rules_rechecked=1, lint_rechecked=False,
                              files=1, lines=4),
    changed_files=("orders/a.py",), calls=1, input_tokens=1200, output_tokens=80,
    models=("claude-haiku-5-5",), attempts=1,
)  # fmt: skip


def _fix_text(view: FixView) -> str:
    found = _found(RuleChecked(RULE, REF), violations=(_violation(),))
    return "\n".join(section(found, {}, None, view))


def test_fix_result_names_the_pull_request_and_the_verification() -> None:
    """수정안 PR 번호와 파이프라인이 확인한 검증 결과가 보인다 (4.9). 아직 안 만든다는 글은 없다."""
    text = _fix_text(FixView(2, 0, "done", fix=READY, proposal=ProposalResult(number=901)))
    assert ("**수정안:** 위반 2건을 고쳤습니다. 검증: 테스트 9개가 수정 전과 같은 결과로 통과, "
            "대상 위반 2건 해소, 새 위반 없음. 수정안 PR #901을 열었습니다.") in text  # fmt: skip
    assert ("수정 작성: 모델 호출 1회, 회차 1 · `claude-haiku-5-5` · 저장된 결과 0건 사용 · "
            "토큰 입력 1200, 출력 80") in text  # fmt: skip
    assert "아직 만들지 않습니다" not in text


def test_fix_states_before_and_without_a_fix(tmp_path) -> None:
    assert "수정안을 만드는 중" in _fix_text(FixView(3, 0, "running"))
    assert "`require_approval: true`" in _fix_text(FixView(3, 0, "approval"))
    assert "모델 수정안 PR #905이 열려 있습니다" in _fix_text(FixView(3, 0, "open", number=905))
    capped = _fix_text(FixView(20, 4, "running"))
    assert "수정안 크기 상한으로 처리하지 못함: 4건" in capped
    failed = replace(READY, status="failed", verification=None, detail="고친 뒤 테스트: 실패")
    text = _fix_text(FixView(2, 0, "done", fix=failed))
    assert (
        "**수정안:** 수정안을 만들지 못함: 정해진 회차 안에 검증을 통과하지 못함 (`고친 뒤" in text
    )
    capped_calls = replace(READY, status="budget", detail="호출 상한: PR 상한 (3/3회 사용)")
    assert "모델 호출 상한에 걸려 처리하지 못함: 수정 2건" in _fix_text(
        FixView(2, 0, "done", fix=capped_calls)
    )


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
