"""app/sandbox/pytest_runner.py 단위 테스트."""

from pathlib import Path

import pytest

from app.models.test_run import ReportSummary
from app.sandbox import docker_cli, pytest_runner
from app.sandbox.container import ExecResult
from app.sandbox.docker_cli import SandboxError


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


# --- 실행 불가(PR 코드 문제)와 환경 오류(SandboxError) 구분 -------------------------


@pytest.fixture
def fake_run(monkeypatch, tmp_path: Path):
    """run_in_sandbox 대신 정해 둔 결과를 돌려주고, 필요하면 report.json을 남긴다."""

    def install(result: ExecResult, *, report: str | None = None, alive: bool = True) -> Path:
        def run(container, argv, **kwargs):
            if report is not None:
                (tmp_path / pytest_runner.REPORT_FILE).write_text(report)
            return result

        (tmp_path / pytest_runner.OUT_DIR).mkdir(exist_ok=True)
        monkeypatch.setattr(pytest_runner, "run_in_sandbox", run)
        monkeypatch.setattr(docker_cli, "container_running", lambda name: alive)
        return tmp_path

    return install


@pytest.mark.parametrize("code", sorted(pytest_runner.DOCKER_EXEC_FAILED))
def test_docker_exec_failure_is_environment_error(fake_run, code: int) -> None:
    worktree = fake_run(ExecResult(returncode=code, stdout="", stderr="exec failed"))
    with pytest.raises(SandboxError, match="docker exec 실패"):
        pytest_runner.run_tests("job-1", worktree)


def test_dead_container_without_report_is_environment_error(fake_run) -> None:
    worktree = fake_run(ExecResult(returncode=1, stdout="", stderr=""), alive=False)
    with pytest.raises(SandboxError, match="실행 중이 아니다"):
        pytest_runner.run_tests("job-1", worktree)


def test_live_container_without_report_is_unrunnable(fake_run) -> None:
    worktree = fake_run(ExecResult(returncode=4, stdout="ImportError in conftest", stderr=""))
    run = pytest_runner.run_tests("job-1", worktree)
    assert run.status == "unrunnable"
    assert "ImportError in conftest" in run.output


def test_oom_kill_with_live_container_is_unrunnable(fake_run) -> None:
    worktree = fake_run(ExecResult(returncode=137, stdout="", stderr="Killed"))
    run = pytest_runner.run_tests("job-1", worktree)
    assert run.unrunnable_reason == "메모리 한도를 넘어 종료됨"


def test_failed_tests_with_report_are_not_environment_error(fake_run) -> None:
    report = '{"summary": {"failed": 1, "total": 1}, "tests": []}'
    worktree = fake_run(ExecResult(returncode=1, stdout="", stderr=""), report=report, alive=False)
    run = pytest_runner.run_tests("job-1", worktree)  # 결과가 있으면 컨테이너 상태와 무관
    assert run.status == "failed"


def test_cancelled_run_is_not_environment_error(fake_run) -> None:
    worktree = fake_run(
        ExecResult(returncode=None, stdout="", stderr="", cancelled=True), alive=False
    )
    assert pytest_runner.run_tests("job-1", worktree).status == "cancelled"
