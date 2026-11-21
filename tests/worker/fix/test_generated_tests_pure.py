"""만든 테스트를 받아들이는 조건과 실행 상태 (docs/DESIGN.md 4.8의 6번, 4.9, 7.9)."""

import json
from dataclasses import replace
from typing import Final

import pytest

from worker.fix.generated_tests_pure import (
    LINE_LABEL,
    MAX_TEST_FILES,
    MAX_TEST_LINES,
    Candidate,
    FileLines,
    GeneratedTest,
    LineRun,
    Suite,
    after_check,
    file_of_test,
    group_lines,
    has_checks,
    judge_tests,
    line_runs,
    parse_contexts,
    parse_report,
    render_before,
    render_check,
)
from worker.mcp_pure import ToolUsage
from worker.test_run_pure import PytestRun, ReportSummary
from worker.violation_pure import Violation

SRC: Final = "pkg/m.py"
SOURCE: Final = (
    "LIMIT = 10\n\n"
    "def f(x):\n"
    "    if x > LIMIT:\n"
    "        return 1\n"
    "    return 0\n\n"
    "def g(x):\n"
    "    return x * 2\n"
)
OK_TEST: Final = "from pkg.m import f\n\ndef test_f():\n    assert f(11) == 1\n"
PASSED: Final = PytestRun(exit_code=0, summary=ReportSummary(passed=2, total=2))
EXISTING: Final = "tests/test_a.py"
NEW: Final = "tests/test_pipeline_f.py"


def violation(line: int, covered: bool | None = False, vid: str = "") -> Violation:
    return Violation(id=vid or f"v{line}", rule_id="R", file=SRC, line=line, message="m",
                     found_by="checker", covered=covered)  # fmt: skip


def suite(by: dict[int, set[str]], outcomes=None, counts=None, run=PASSED) -> Suite:
    lines = FileLines(frozenset({1, 3, 4, 5, 6, 8, 9}), {n: frozenset(t) for n, t in by.items()})
    return Suite(
        run=run,
        outcomes=outcomes if outcomes is not None else {EXISTING: "passed", NEW: "passed"},
        counts=counts if counts is not None else {EXISTING: 1, NEW: 1},
        lines={SRC: lines},
    )


# 있던 테스트는 6줄(return 0)을, 만든 테스트는 4·5줄(if, return 1)을 실행한다
BEFORE: Final = suite({1: {""}, 3: {""}, 8: {""}, 4: {EXISTING, NEW}, 6: {EXISTING}, 5: {NEW}})


TARGETS: Final = (violation(5),)


def judge(candidates, targets=TARGETS, before=BEFORE, **kw):
    return judge_tests(candidates, targets, before, {SRC: SOURCE}, **kw)


def test_a_test_that_passes_before_and_newly_runs_the_violation_line_is_accepted() -> None:
    (t,) = judge([Candidate(NEW, OK_TEST)])
    assert t.accepted and t.before == "passed" and t.covers == ("v5",) and t.new_lines == 1


@pytest.mark.parametrize(
    ("source", "before", "reason"),
    [
        ("def test_f():\n    f(11)\n", BEFORE, "empty"),
        ("x = 1\n", BEFORE, "empty"),
        ("def test_f(:\n", BEFORE, "empty"),
        (OK_TEST, suite({5: {NEW}}, outcomes={NEW: "failed"}), "fails_before"),
        (OK_TEST, suite({5: {NEW}}, outcomes={}, counts={}), "not_run"),
        (OK_TEST, suite({9: {NEW}, 5: {EXISTING}}), "misses_violation"),
        (OK_TEST, suite({4: {EXISTING, NEW}, 5: {EXISTING, NEW}}), "overlaps"),
        ("def test_f():\n    assert True\n" + "#\n" * MAX_TEST_LINES, BEFORE, "too_big"),
    ],
    ids=[
        "no-assert",
        "no-test-function",
        "syntax-error",
        "fails-before",
        "not-collected",
        "misses-violation",
        "overlaps-existing",
        "too-big",
    ],  # fmt: skip
)
def test_each_failed_condition_rejects_with_its_name(source, before, reason) -> None:
    (t,) = judge([Candidate(NEW, source)], before=before)
    assert t.rejected == reason and not t.accepted


def test_overlapping_the_violation_line_of_an_existing_test_needs_other_new_lines() -> None:
    """위반 줄을 있던 테스트도 실행하면, 다른 새 줄을 실행해야 받는다."""
    covered = violation(5, covered=True)
    before = suite({4: {EXISTING, NEW}, 5: {EXISTING, NEW}, 9: {NEW}})
    (t,) = judge([Candidate(NEW, OK_TEST)], targets=(covered,), before=before)
    assert t.accepted and t.new_lines == 1


def test_a_second_test_that_runs_only_lines_taken_by_the_first_overlaps() -> None:
    """겹치지 않음: 앞서 받아들인 테스트가 실행한 줄만 다시 실행하는 테스트는 받지 않는다."""
    other = "tests/test_pipeline_g.py"
    before = suite({4: {NEW, other}, 5: {NEW, other}, 6: {EXISTING}},
                   outcomes={EXISTING: "passed", NEW: "passed", other: "passed"},
                   counts={EXISTING: 1, NEW: 1, other: 1})  # fmt: skip
    first, second = judge([Candidate(other, OK_TEST), Candidate(NEW, OK_TEST)], before=before)
    assert (first.file, first.accepted) == (NEW, True)
    assert (second.file, second.rejected) == (other, "overlaps")


def test_a_test_kept_in_an_earlier_round_is_judged_first() -> None:
    """회차를 넘어: 앞 회차에 받아들인 파일을 먼저 보므로 새 파일이 그 줄을 다시 덮으면 떨어진다."""
    other = "tests/test_pipeline_a.py"
    before = suite({4: {NEW, other}, 5: {NEW, other}, 6: {EXISTING}},
                   outcomes={EXISTING: "passed", NEW: "passed", other: "passed"},
                   counts={EXISTING: 1, NEW: 1, other: 1})  # fmt: skip
    tests = judge([Candidate(other, OK_TEST), Candidate(NEW, OK_TEST)], before=before, kept=[NEW])
    assert [(t.file, t.rejected) for t in tests] == [(NEW, None), (other, "overlaps")]


def test_only_max_files_are_accepted() -> None:
    names = [f"tests/test_pipeline_{i:02d}.py" for i in range(MAX_TEST_FILES + 1)]
    by = {4: set(names), 6: {EXISTING}}
    by.update({100 + i: {n} for i, n in enumerate(names)})  # 파일마다 자기만 실행한 줄이 있다
    by[5] = set(names)
    before = Suite(
        run=PASSED, outcomes=dict.fromkeys(names, "passed"), counts=dict.fromkeys(names, 1),
        lines={SRC: FileLines(frozenset(by), {n: frozenset(t) for n, t in by.items()})},
    )  # fmt: skip
    tests = judge([Candidate(n, OK_TEST) for n in names], before=before)
    assert [t.rejected for t in tests] == [None] * MAX_TEST_FILES + ["too_many"]


def test_import_time_lines_count_only_for_a_test_that_also_runs_the_file() -> None:
    """모듈을 불러올 때 실행된 줄("")은 그 파일의 다른 줄을 실행한 테스트의 것으로만 본다."""
    top = violation(1)
    runs_file = suite({1: {""}, 9: {NEW}})
    only_imports = suite({1: {""}})
    assert judge([Candidate(NEW, OK_TEST)], targets=(top,), before=runs_file)[0].accepted
    (t,) = judge([Candidate(NEW, OK_TEST)], targets=(top,), before=only_imports)
    assert t.rejected == "misses_violation"


def test_has_checks_accepts_raises_and_unittest_asserts() -> None:
    assert has_checks("import pytest\ndef test_x():\n    with pytest.raises(ValueError):\n"
                      "        int('a')\n")  # fmt: skip
    assert has_checks("class TestX:\n    def test_y(self):\n        self.assertEqual(1, 1)\n")
    assert not has_checks("def test_x():\n    assert 1\ndef test_y():\n    pass\n")


def test_after_check_keeps_accepted_tests_that_still_pass() -> None:
    (t,) = judge([Candidate(NEW, OK_TEST)])
    before = PytestRun(exit_code=0, summary=ReportSummary(passed=1, total=1))
    after = suite({}, run=PASSED)
    check = after_check([t], before, after)
    assert check.ok and check.tests[0].after == "passed"


def test_a_generated_test_that_breaks_after_the_fix_sends_the_fix_back() -> None:
    """고치기 전에 통과한 테스트가 고친 뒤 실패하면 수정이 동작을 바꾼 것이다. 테스트를 빼지
    않는다."""
    (t,) = judge([Candidate(NEW, OK_TEST)])
    before = PytestRun(exit_code=0, summary=ReportSummary(passed=1, total=1))
    failed = PytestRun(
        exit_code=1,
        summary=ReportSummary(passed=1, failed=1, total=2, failed_tests=(f"{NEW}::test_f",)),
    )
    check = after_check([t], before, suite({}, outcomes={NEW: "failed"}, run=failed))
    assert not check.ok and check.broken == (NEW,) and check.tests[0].accepted


def test_after_check_needs_the_same_existing_tests() -> None:
    before = PytestRun(exit_code=0, summary=ReportSummary(passed=3, total=3))
    check = after_check([], before, suite({}, run=PASSED))
    assert not check.existing_ok and not check.ok


def test_rejected_tests_are_not_run_after() -> None:
    rejected = GeneratedTest(NEW, 3, "failed", rejected="fails_before")
    before = PytestRun(exit_code=0, summary=ReportSummary(passed=2, total=2))
    check = after_check([rejected], before, suite({}, run=PASSED))
    assert check.ok and check.tests == (rejected,)


def test_line_runs_name_three_states_and_carry_the_models_reason() -> None:
    targets = (violation(4, covered=True), violation(5), violation(6), violation(9, None))
    tests = [GeneratedTest(NEW, 3, "passed", covers=("v5",))]
    runs = line_runs(targets, tests, {(SRC, "R"): "파일을 쓰는 최상위 코드"})
    assert [(r.status, r.why) for r in runs] == [
        ("existing", ""), ("generated", ""), ("none", "파일을 쓰는 최상위 코드"),
        ("unknown", "파일을 쓰는 최상위 코드"),
    ]  # fmt: skip


def test_line_runs_ignore_rejected_tests() -> None:
    tests = [GeneratedTest(NEW, 3, "failed", covers=("v5",), rejected="fails_before")]
    assert line_runs([violation(5)], tests)[0].status == "none"


def test_group_lines_folds_lines_by_status_and_file() -> None:
    other = Violation(id="o", rule_id="R", file="b.py", line=2, message="m", found_by="checker")
    vs = [violation(4), violation(5), violation(9), other]
    runs = [LineRun("v4", "none"), LineRun("v5", "generated"), LineRun("v9", "generated"),
            LineRun("o", "generated")]  # fmt: skip
    assert group_lines(runs, vs) == [
        ("generated", SRC, (5, 9), ""), ("generated", "b.py", (2,), ""), ("none", SRC, (4,), ""),
    ]  # fmt: skip


def test_the_old_label_that_read_as_no_tests_at_all_is_gone() -> None:
    assert "이 줄을 실행하는 테스트가 없음" not in LINE_LABEL.values()
    assert len({"existing", "generated", "none"} & set(LINE_LABEL)) == 3


def test_parse_report_groups_tests_by_file() -> None:
    data = {
        "tests": [
            {"nodeid": "tests/test_a.py::test_x", "outcome": "passed"},
            {"nodeid": "tests/test_a.py::T::test_y", "outcome": "failed"},
            {"nodeid": "tests/test_b.py::test_z", "outcome": "passed"},
            {"nodeid": 3},
        ],
        "collectors": [{"nodeid": "tests/test_pipeline_c.py", "outcome": "failed"}],
    }
    outcomes, counts = parse_report(data)
    assert outcomes == {"tests/test_a.py": "failed", "tests/test_b.py": "passed",
                        "tests/test_pipeline_c.py": "failed"}  # fmt: skip
    assert counts == {"tests/test_a.py": 2, "tests/test_b.py": 1}
    assert parse_report("x") == ({}, {})


def test_parse_contexts_reads_test_files_per_line() -> None:
    report = {"files": {SRC: {
        "executed_lines": [1, 4], "missing_lines": [5],
        "contexts": {"1": [""],
                     "4": ["tests/test_a.py::test_f|run", "tests/test_a.py::test_g|run"]},
    }}}  # fmt: skip
    lines = parse_contexts(json.dumps(report).encode(), [SRC, "other.py"])
    assert lines == {
        SRC: FileLines(
            frozenset({1, 4, 5}), {1: frozenset({""}), 4: frozenset({"tests/test_a.py"})}
        )
    }
    assert parse_contexts(b"{", [SRC]) is None
    assert parse_contexts(b'{"files": {"pkg/m.py": {"contexts": {"x": []}}}}', [SRC]) is None


def test_file_of_test_strips_class_and_phase() -> None:
    assert file_of_test("tests/test_a.py::TestX::test_y|run") == "tests/test_a.py"


def test_tool_results_keep_the_reason_and_note_model_found_violations() -> None:
    text = render_check([violation(5)], [], [], by_model=2)
    assert "남은 대상 위반 1건" in text and "도구로 다시 확인할 수 없다" in text
    tests = judge([Candidate(NEW, "def test_f():\n    f(1)\n")])
    shown = render_before(tests, BEFORE, [violation(5)])
    assert "받지 않음 (assert나 pytest.raises가 없는 테스트 함수가 있음)" in shown


def test_tool_usage_adds_up() -> None:
    total = ToolUsage(1, 2, 3.0) + ToolUsage(0, 1, 1.5, limited=True)
    assert (total.calls, total.seconds, total.limited) == (4, 4.5, True)


def test_judge_without_candidates_is_empty() -> None:
    assert judge([]) == ()
    assert replace(BEFORE, lines={}).lines == {}
