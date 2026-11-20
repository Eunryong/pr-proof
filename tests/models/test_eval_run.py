from typing import Final

import pytest

from app.models.eval_run import (
    CaseResult,
    RunSummary,
    compare,
    parse_junit,
    prompt_versions,
    render_comparison,
    render_summary,
)

JUNIT: Final = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest">
<testcase classname="tests.e2e.test_rules_cases" name="test_rules_case[01-print-in-orders]"
  time="12.31"/>
<testcase classname="tests.e2e.test_rules_cases" name="test_rules_case[02-import-direction]"
  time="3"><failure message="x">boom</failure></testcase>
<testcase classname="tests.e2e.test_judge_cases" name="test_judge_case[01-verb-function-name]"
  time="1"><skipped message="real only"/></testcase>
<testcase classname="tests.e2e.test_rag_cases" name="test_rag_case[03-other-repo-not-searched]"
  time="1"><error message="setup">bad</error></testcase>
<testcase classname="tests.e2e.test_tools_off_eval"
  name="test_tools_on_and_off_score_the_same_samples" time="40"/>
<testcase classname="tests.e2e.test_rag_compare"
  name="test_search_off_and_on[01-adopted-constants-file-fix]" time="40"/>
</testsuite></testsuites>
"""


def test_junit_cases_map_to_case_files_and_other_tests_keep_their_names() -> None:
    cases = {c.case: c for c in parse_junit(JUNIT)}
    assert cases["rules/01-print-in-orders"] == CaseResult("rules/01-print-in-orders", "passed",
                                                           12.3)  # fmt: skip
    assert cases["rules/02-import-direction"].outcome == "failed"
    assert cases["judge/01-verb-function-name"].outcome == "skipped"
    assert cases["rag/03-other-repo-not-searched"].outcome == "failed"  # 준비 오류도 실패다
    other = "test_tools_off_eval::test_tools_on_and_off_score_the_same_samples"
    assert cases[other].outcome == "passed" and not cases[other].is_case_file
    # 케이스 파일 하네스가 아닌 매개변수 테스트는 케이스로 세지 않는다
    assert "test_rag_compare::test_search_off_and_on[01-adopted-constants-file-fix]" in cases


def test_same_case_twice_keeps_the_worse_outcome() -> None:
    xml = (
        '<testsuite><testcase classname="tests.e2e.test_lint_cases" name="test_lint_case[01-a]"/>'
        '<testcase classname="tests.e2e.test_lint_cases" name="test_lint_case[01-a]">'
        "<failure/></testcase></testsuite>"
    )
    (only,) = parse_junit(xml)
    assert only.outcome == "failed"


def test_empty_junit_has_no_cases_and_broken_junit_is_an_error() -> None:
    assert parse_junit("<testsuites/>") == ()
    with pytest.raises(Exception):  # noqa: B017, PT011
        parse_junit("<testsuite>")


def _summary(*cases: tuple[str, str], tools: dict | None = None, prompts: dict | None = None,
             models: dict | None = None) -> RunSummary:  # fmt: skip
    return RunSummary(
        cases=tuple(CaseResult(c, o) for c, o in cases),  # type: ignore[arg-type]
        tools_off=tools or {},
        prompts=prompts or {"fix": "a"},
        models=models or {"light": "haiku"},
    )


def test_compare_lists_regressions_fixes_and_changed_settings() -> None:
    base = _summary(("rules/01", "passed"), ("rules/02", "failed"), ("rules/03", "passed"),
                    ("lint/01", "passed"), tools={"off/checker": {"recall": "8/9",
                                                                  "precision": "8/8"}})  # fmt: skip
    new = _summary(("rules/01", "failed"), ("rules/02", "passed"), ("rules/03", "skipped"),
                   ("rules/04", "passed"), tools={"off/checker": {"recall": "9/9",
                                                                  "precision": "8/8"},
                                                  "off/judge": {"recall": "2/2",
                                                                "precision": "2/2"}},
                   prompts={"fix": "b"}, models={"light": "sonnet"})  # fmt: skip
    c = compare(base, new)
    assert c.newly_failed == ("rules/01",)
    assert c.newly_passed == ("rules/02",)
    assert [(x.key, x.before, x.after) for x in c.other_changes] == [
        ("rules/03", "passed", "skipped")
    ]
    assert c.added == ("rules/04",) and c.removed == ("lint/01",)
    assert [(x.key, x.before, x.after) for x in c.tools_off] == [
        ("off/checker recall", "8/9", "9/9"),
        ("off/judge recall", "-", "2/2"),
        ("off/judge precision", "-", "2/2"),
    ]
    assert [(x.key, x.before, x.after) for x in c.settings] == [
        ("prompt.fix", "a", "b"),
        ("model.light", "haiku", "sonnet"),
    ]
    assert c.regressed
    text = render_comparison(c, base_id=7)
    assert "기준선 #7과 비교:" in text
    assert "새로 실패 1개: rules/01" in text
    assert "  off/checker recall: 8/9 → 9/9" in text
    assert "  prompt.fix: a → b" in text


def test_identical_runs_compare_to_nothing() -> None:
    run = _summary(("rules/01", "passed"))
    c = compare(run, run)
    assert not c.regressed
    assert render_comparison(c, base_id=1).endswith("바뀐 것 없음")


def test_summary_counts_case_files_apart_from_other_tests() -> None:
    run = _summary(("rules/01", "passed"), ("rules/02", "failed"), ("judge/01", "skipped"),
                   ("test_tools_off_eval::t", "passed"))  # fmt: skip
    text = render_summary(run)
    assert "평가 케이스 3개 · 통과 1 · 실패 1 · 건너뜀 1" in text
    assert "그 밖의 평가 테스트 1개 · 실패 0" in text
    assert "실패: rules/02" in text
    assert "도구를 끈 평가: 결과 없음" in text


def test_every_model_stage_has_a_prompt_version() -> None:
    versions = prompt_versions()
    assert set(versions) == {"rules", "tool_choice", "checker", "checker_path", "judge",
                             "decide", "fix"}  # fmt: skip
    assert all(versions.values())
