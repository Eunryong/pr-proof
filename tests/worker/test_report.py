from typing import Final

import pytest

from app.models.test_run import PytestRun, ReportSummary
from app.worker import report
from app.worker.report import format_test_line, render_test_section

SYMBOLS: Final = ("⚠️", "✅", "❌")


@pytest.mark.parametrize(
    ("run", "line"),
    [
        (PytestRun(exit_code=0, summary=ReportSummary(passed=9, total=9)), "통과 (9개)"),
        (
            PytestRun(exit_code=1, summary=ReportSummary(passed=7, failed=1, errors=1, total=9)),
            "실패 (9개 중 2개)",
        ),
        (PytestRun(exit_code=5, summary=ReportSummary()), "실행된 테스트 없음"),
        (PytestRun(exit_code=124, summary=None, timed_out=True), "시간 초과로 중단"),
        (PytestRun(exit_code=None, summary=None, cancelled=True), "새 커밋이 올라와 중단"),
        (PytestRun(exit_code=137, summary=None), "실행 불가 (메모리 한도를 넘어 종료됨)"),
    ],
    ids=["passed", "failed", "no_tests", "timeout", "cancelled", "unrunnable"],
)
def test_test_line_is_text_only(run: PytestRun, line: str) -> None:
    assert format_test_line(run) == line
    text = render_test_section(run)
    assert text.startswith(f"**테스트:** {line}\n")
    assert not any(symbol in text for symbol in SYMBOLS)  # 기호는 규칙 줄에만 쓴다


def test_passed_shows_counts_without_failures() -> None:
    text = render_test_section(PytestRun(exit_code=0, summary=ReportSummary(passed=9, total=9)))
    assert "전체 9 · 통과 9 · 실패 0 · 오류 0 · 건너뜀 0" in text
    assert "실패한 테스트" not in text


def test_failed_lists_tests() -> None:
    summary = ReportSummary(passed=1, failed=1, total=2, failed_tests=("tests/test_a.py::test_x",))
    text = render_test_section(PytestRun(exit_code=1, summary=summary))
    assert "- `tests/test_a.py::test_x`" in text


def test_collection_error_lists_module() -> None:
    summary = ReportSummary(errors=1, failed_tests=("tests/test_broken.py",))
    text = render_test_section(PytestRun(exit_code=2, summary=summary))
    assert text.startswith("**테스트:** 실패 (0개 중 1개)")
    assert "- `tests/test_broken.py`" in text


def test_long_failure_list_is_truncated() -> None:
    count = report.MAX_LISTED_FAILURES + 3
    ids = tuple(f"tests/test_a.py::test_{i}" for i in range(count))
    run = PytestRun(exit_code=1, summary=ReportSummary(failed=count, total=count, failed_tests=ids))
    text = render_test_section(run)
    assert f"`tests/test_a.py::test_{report.MAX_LISTED_FAILURES - 1}`" in text
    assert f"`tests/test_a.py::test_{report.MAX_LISTED_FAILURES}`" not in text
    assert "- 외 3개" in text


def test_unrunnable_shows_output_without_counts() -> None:
    run = PytestRun(exit_code=4, summary=None, output="ERROR: usage")
    text = render_test_section(run)
    assert "실행 불가 (pytest 설정·인자 오류)" in text
    assert "ERROR: usage" in text
    assert "전체" not in text


def test_timeout_hides_output() -> None:
    run = PytestRun(exit_code=124, summary=None, timed_out=True, output="x")
    assert "<details>" not in render_test_section(run)
