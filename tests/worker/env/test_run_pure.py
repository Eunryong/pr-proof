"""app/worker/env/run_pure.py 단위 테스트: 테스트 단계의 판정."""

import pytest

from app.worker.env.run_pure import judge_test

PATH = ".pipeline-out/junit.xml"
PASSED = b'<testsuite><testcase classname="a" name="one"/><testcase name="two"/></testsuite>'
FAILED = b'<testsuite><testcase classname="a" name="one"><failure/></testcase></testsuite>'
ERROR = b'<testsuites><testsuite><testcase name="x"><error/></testcase></testsuite></testsuites>'
EMPTY = b"<testsuites/>"


@pytest.mark.parametrize(
    ("exit_code", "junit", "verdict"),
    [
        (0, PASSED, "passed"),
        (8, FAILED, "failed"),
        (0, FAILED, "failed"),
        (8, ERROR, "failed"),
        (0, EMPTY, "no_tests"),
        (8, EMPTY, "no_tests"),
    ],
)
def test_junit_verdicts(exit_code: int, junit: bytes, verdict: str) -> None:
    got, why, report = judge_test("junit", exit_code, junit, PATH)
    assert (got, why) == (verdict, "")
    assert report is not None


def test_junit_counts_and_failing_names() -> None:
    _, _, report = judge_test("junit", 8, FAILED, PATH)
    assert (report.total, report.failed, report.failed_tests) == (1, 1, ("a.one",))


@pytest.mark.parametrize("junit", [None, b"", b"not xml", b"<html/>", b"<!DOCTYPE x><testsuite/>"])
def test_missing_or_unreadable_junit_is_unrunnable_with_exit_code(junit: bytes | None) -> None:
    verdict, why, report = judge_test("junit", 2, junit, PATH)
    assert verdict == "unrunnable" and report is None
    assert PATH in why and "종료 코드 2" in why


def test_all_passed_but_nonzero_exit_is_unrunnable() -> None:
    verdict, why, report = judge_test("junit", 1, PASSED, PATH)
    assert verdict == "unrunnable" and "종료 코드 1" in why and report.passed == 2


@pytest.mark.parametrize(("exit_code", "verdict"), [(0, "passed"), (1, "failed"), (None, "failed")])
def test_exit_report_reads_only_the_exit_code(exit_code: int | None, verdict: str) -> None:
    assert judge_test("exit", exit_code, PASSED, "") == (verdict, "", None)
