from typing import Final

import pytest

from app.models.test_run import PytestRun, ReportSummary


def test_summary_counts_failures_and_collection_errors() -> None:
    data = {
        "summary": {"passed": 2, "failed": 1, "error": 1, "skipped": 1, "total": 5},
        "tests": [
            {"nodeid": "tests/test_a.py::test_ok", "outcome": "passed"},
            {"nodeid": "tests/test_a.py::test_bad", "outcome": "failed"},
            {"nodeid": "tests/test_a.py::test_fixture", "outcome": "error"},
            {"nodeid": "tests/test_a.py::test_skip", "outcome": "skipped"},
        ],
        "collectors": [
            {"nodeid": "", "outcome": "passed"},
            {"nodeid": "tests/test_broken.py", "outcome": "failed"},
        ],
    }
    assert ReportSummary.from_report(data) == ReportSummary(
        passed=2,
        failed=1,
        errors=2,
        skipped=1,
        total=5,
        failed_tests=(
            "tests/test_a.py::test_bad",
            "tests/test_a.py::test_fixture",
            "tests/test_broken.py",
        ),
    )


def test_summary_of_empty_report() -> None:
    assert ReportSummary.from_report({}) == ReportSummary()


SUMMARY: Final = ReportSummary(passed=1, total=1)


@pytest.mark.parametrize(
    ("run", "status"),
    [
        (PytestRun(exit_code=0, summary=SUMMARY), "passed"),
        (PytestRun(exit_code=1, summary=SUMMARY), "failed"),
        (PytestRun(exit_code=2, summary=SUMMARY), "failed"),  # 수집 오류
        (PytestRun(exit_code=5, summary=ReportSummary()), "no_tests"),
        (PytestRun(exit_code=124, summary=None, timed_out=True), "timeout"),
        (PytestRun(exit_code=None, summary=None, cancelled=True), "cancelled"),
        (PytestRun(exit_code=0, summary=None), "error"),  # 결과 파일 없음
        (PytestRun(exit_code=1, summary=None), "error"),
        (PytestRun(exit_code=4, summary=SUMMARY), "error"),  # 사용법 오류
    ],
)
def test_status(run: PytestRun, status: str) -> None:
    assert run.status == status
    assert run.ok is (status == "passed")
