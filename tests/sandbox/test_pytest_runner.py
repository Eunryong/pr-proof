"""app/sandbox/pytest_runner.py 단위 테스트."""

from pathlib import Path

from app.models.test_run import ReportSummary
from app.sandbox import pytest_runner


def test_pythonpath_for_src_layout(tmp_path: Path) -> None:
    assert pytest_runner.pythonpath_for(tmp_path) == "/work"
    (tmp_path / "src").mkdir()
    assert pytest_runner.pythonpath_for(tmp_path) == "/work/src:/work"


def test_pytest_argv_writes_outputs_under_pipeline_out() -> None:
    argv = pytest_runner.pytest_argv()
    assert "--json-report-file=.pipeline-out/report.json" in argv
    assert "--cov-report=xml:.pipeline-out/coverage.xml" in argv
    assert "--cov-config=/pipeline/coveragerc" in argv


def test_read_summary_missing_or_broken(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    assert pytest_runner.read_summary(report) is None
    report.write_text("{not json")
    assert pytest_runner.read_summary(report) is None
    report.write_text('{"summary": {"passed": 1, "total": 1}}')
    assert pytest_runner.read_summary(report) == ReportSummary(passed=1, total=1)
