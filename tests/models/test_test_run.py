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
        (PytestRun(exit_code=0, summary=None), "unrunnable"),  # 결과 파일 없음
        (PytestRun(exit_code=1, summary=None), "unrunnable"),
        (PytestRun(exit_code=4, summary=SUMMARY), "unrunnable"),  # 사용법 오류
        (PytestRun(exit_code=137, summary=None), "unrunnable"),  # OOM
        (PytestRun(exit_code=137, summary=None, cancelled=True), "cancelled"),
        (PytestRun(exit_code=137, summary=None, timed_out=True), "timeout"),
    ],
)
def test_status(run: PytestRun, status: str) -> None:
    assert run.status == status
    assert run.ok is (status == "passed")


@pytest.mark.parametrize(
    ("run", "reason"),
    [
        (PytestRun(exit_code=137, summary=None), "메모리 한도를 넘어 종료됨"),
        (PytestRun(exit_code=3, summary=SUMMARY), "pytest 내부 오류"),
        (PytestRun(exit_code=4, summary=None), "pytest 설정·인자 오류"),
        (PytestRun(exit_code=1, summary=None), "결과 파일 없음 (conftest 등 수집 전 오류)"),
        (PytestRun(exit_code=0, summary=SUMMARY), None),  # 실행 불가가 아님
    ],
)
def test_unrunnable_reason(run: PytestRun, reason: str | None) -> None:
    assert run.unrunnable_reason == reason


@pytest.mark.parametrize(
    ("run", "lint", "code"),
    [
        (PytestRun(exit_code=0, summary=SUMMARY), True, True),
        (PytestRun(exit_code=5, summary=ReportSummary()), True, False),  # 안전망 없음
        (PytestRun(exit_code=1, summary=SUMMARY), False, False),
        (PytestRun(exit_code=124, summary=None, timed_out=True), False, False),
        (PytestRun(exit_code=4, summary=None), False, False),
        (PytestRun(exit_code=None, summary=None, cancelled=True), False, False),
    ],
    ids=["passed", "no_tests", "failed", "timeout", "unrunnable", "cancelled"],
)
def test_fix_permissions_follow_safety_net(run: PytestRun, lint: bool, code: bool) -> None:
    assert (run.allows_lint_fix, run.allows_code_fix) == (lint, code)


@pytest.mark.parametrize(
    ("run", "exceeded"),
    [
        (PytestRun(exit_code=137, summary=None), True),
        (PytestRun(exit_code=1, summary=None, oom_killed=True), True),
        (PytestRun(exit_code=137, summary=None, cancelled=True), False),  # 파이프라인이 지움
        (PytestRun(exit_code=137, summary=None, timed_out=True), False),
        (PytestRun(exit_code=4, summary=None), False),
    ],
    ids=["exit-137", "oom-flag", "cancelled", "timed_out", "usage-error"],
)
def test_memory_exceeded_checks_cancel_and_timeout_first(run: PytestRun, exceeded: bool) -> None:
    assert run.memory_exceeded is exceeded
