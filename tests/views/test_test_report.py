from app.models.test_run import PytestRun, ReportSummary
from app.views import test_report
from app.views.test_report import render_test_section


def test_passed() -> None:
    run = PytestRun(exit_code=0, summary=ReportSummary(passed=9, total=9))
    text = render_test_section(run)
    assert "**테스트 통과**" in text
    assert "전체 9 · 통과 9 · 실패 0 · 오류 0 · 건너뜀 0" in text
    assert "실패한 테스트" not in text


def test_failed_lists_tests() -> None:
    summary = ReportSummary(passed=1, failed=1, total=2, failed_tests=("tests/test_a.py::test_x",))
    text = render_test_section(PytestRun(exit_code=1, summary=summary))
    assert "**❌ 테스트 실패**" in text
    assert "- `tests/test_a.py::test_x`" in text


def test_long_failure_list_is_truncated() -> None:
    count = test_report.MAX_LISTED_FAILURES + 3
    ids = tuple(f"tests/test_a.py::test_{i}" for i in range(count))
    run = PytestRun(exit_code=1, summary=ReportSummary(failed=count, total=count, failed_tests=ids))
    text = render_test_section(run)
    assert f"`tests/test_a.py::test_{test_report.MAX_LISTED_FAILURES - 1}`" in text
    assert f"`tests/test_a.py::test_{test_report.MAX_LISTED_FAILURES}`" not in text
    assert "- 외 3개" in text


def test_error_shows_output_without_counts() -> None:
    run = PytestRun(exit_code=4, summary=None, output="ERROR: usage")
    text = render_test_section(run)
    assert "pytest를 실행하지 못함" in text
    assert "ERROR: usage" in text
    assert "전체" not in text


def test_timeout() -> None:
    run = PytestRun(exit_code=124, summary=None, timed_out=True, output="x")
    text = render_test_section(run)
    assert "시간 초과" in text and "<details>" not in text
