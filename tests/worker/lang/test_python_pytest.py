"""app/worker/lang/python.py의 pytest 실행과 결과 읽기 단위 테스트."""

import json
from pathlib import Path
from typing import Final

import pytest

from app.worker.lang import python
from app.worker.sandbox import docker_cli
from app.worker.sandbox.container import ExecResult
from app.worker.sandbox.docker_cli import SandboxError
from app.worker.testing.test_run_pure import StepRun, TestReport


def test_pythonpath_for_src_layout(tmp_path: Path) -> None:
    assert python.pythonpath_for(tmp_path) == "/work"
    (tmp_path / "src").mkdir()
    assert python.pythonpath_for(tmp_path) == "/work/src:/work"


def test_pytest_argv_writes_outputs_under_pipeline_out() -> None:
    argv = python.pytest_argv()
    assert "--json-report-file=.pipeline-out/report.json" in argv
    assert "--cov-report=xml:.pipeline-out/coverage.xml" in argv
    assert "--cov-config=/pipeline/coveragerc" in argv


def test_read_report_missing_or_broken(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    assert python.read_report(report) is None
    report.write_text("{not json")
    assert python.read_report(report) is None
    report.write_text('{"summary": {"passed": 1, "total": 1}}')
    assert python.read_report(report) == TestReport(passed=1, total=1)


def test_report_counts_failures_and_collection_errors() -> None:
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
    assert python.report_of(data) == TestReport(
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
    assert python.report_of({}) == TestReport()


ONE: Final = TestReport(passed=1, total=1)


@pytest.mark.parametrize(
    ("code", "report", "status", "reason"),
    [
        (0, ONE, "passed", None),
        (1, ONE, "failed", None),
        (2, ONE, "failed", None),  # 수집 오류
        (5, TestReport(), "no_tests", None),
        (5, None, "no_tests", None),
        (0, None, "unrunnable", "결과 파일 없음 (conftest 등 수집 전 오류)"),
        (1, None, "unrunnable", "결과 파일 없음 (conftest 등 수집 전 오류)"),
        (3, ONE, "unrunnable", "pytest 내부 오류"),
        (4, ONE, "unrunnable", "pytest 설정·인자 오류"),
        (137, None, "unrunnable", "메모리 한도를 넘어 종료됨"),
        (139, ONE, "unrunnable", "pytest 종료 코드 139"),
    ],
)
def test_pytest_exit_code_decides_the_test_step(
    code: int, report: TestReport | None, status: str, reason: str | None
) -> None:
    run = python.pytest_result(StepRun(python.PYTEST_STEP, code), report)
    assert (run.status, run.unrunnable_reason) == (status, reason)


def test_pytest_failures_that_all_pass_on_rerun_count_as_passed() -> None:
    report = TestReport(passed=8, failed=1, total=9, failed_tests=("t::a",))
    run = python.pytest_result(StepRun(python.PYTEST_STEP, 1), report, flaky=("t::a",))
    assert run.status == "passed" and run.passed_count == 9 and not run.allows_code_fix


# --- 실행 불가(PR 코드 문제)와 환경 오류(SandboxError) 구분 -------------------------


RUNNING: Final = docker_cli.ContainerState(running=True, oom_killed=False)
STOPPED: Final = docker_cli.ContainerState(running=False, oom_killed=False)
OOM_KILLED: Final = docker_cli.ContainerState(running=False, oom_killed=True)


@pytest.fixture
def fake_run(monkeypatch, tmp_path: Path):
    """run_in_sandbox 대신 정해 둔 결과를 돌려주고, 필요하면 report.json을 남긴다."""

    def install(
        result: ExecResult,
        *,
        report: str | None = None,
        state: docker_cli.ContainerState | None = RUNNING,
    ) -> Path:
        def run(container, argv, **kwargs):
            if report is not None:
                (tmp_path / python.REPORT_FILE).write_text(report)
            return result

        (tmp_path / python.OUT_DIR).mkdir(exist_ok=True)
        monkeypatch.setattr(python, "run_in_sandbox", run)
        monkeypatch.setattr(docker_cli, "container_state", lambda name: state)
        return tmp_path

    return install


@pytest.mark.parametrize("code", sorted(docker_cli.DOCKER_FAILED))
def test_docker_exec_failure_is_environment_error(fake_run, code: int) -> None:
    worktree = fake_run(ExecResult(returncode=code, stdout="", stderr="exec failed"))
    with pytest.raises(SandboxError, match="docker exec 실패"):
        python.run_tests("job-1", worktree)


@pytest.mark.parametrize("state", [STOPPED, None], ids=["stopped", "gone"])
def test_dead_container_without_report_is_environment_error(fake_run, state) -> None:
    worktree = fake_run(ExecResult(returncode=1, stdout="", stderr=""), state=state)
    with pytest.raises(SandboxError, match="실행 중이 아니다"):
        python.run_tests("job-1", worktree)


def test_live_container_without_report_is_unrunnable(fake_run) -> None:
    worktree = fake_run(ExecResult(returncode=4, stdout="ImportError in conftest", stderr=""))
    run = python.run_tests("job-1", worktree)
    assert run.status == "unrunnable"
    assert "ImportError in conftest" in run.output


def test_oom_kill_inside_live_container_is_memory_exceeded(fake_run) -> None:
    worktree = fake_run(ExecResult(returncode=137, stdout="", stderr="Killed"))
    run = python.run_tests("job-1", worktree)
    assert run.memory_exceeded and not run.test.oom_killed


def test_oom_killed_container_is_unrunnable_not_environment(fake_run) -> None:
    """메모리 한도로 컨테이너 자체가 죽어도 환경 오류가 아니라 PR 코드 문제다."""
    worktree = fake_run(ExecResult(returncode=137, stdout="", stderr=""), state=OOM_KILLED)
    run = python.run_tests("job-1", worktree)
    assert run.status == "unrunnable" and run.test.oom_killed and run.memory_exceeded


def test_failed_tests_with_report_are_not_environment_error(fake_run) -> None:
    report = '{"summary": {"failed": 1, "total": 1}, "tests": []}'
    worktree = fake_run(ExecResult(returncode=1, stdout="", stderr=""), report=report, state=None)
    run = python.run_tests("job-1", worktree)  # 결과가 있으면 컨테이너 상태와 무관
    assert run.status == "failed"


@pytest.mark.parametrize(
    "result",
    [
        ExecResult(returncode=None, stdout="", stderr="", cancelled=True),
        ExecResult(returncode=137, stdout="", stderr="", timed_out=True),
    ],
    ids=["cancelled", "timed_out-137"],
)
def test_pipeline_removal_is_not_environment_or_memory(fake_run, result) -> None:
    """취소·시간 초과로 파이프라인이 컨테이너를 지운 경우 137이어도 메모리 초과가 아니다."""
    worktree = fake_run(result, state=None)
    run = python.run_tests("job-1", worktree)
    assert run.status in ("cancelled", "timeout") and not run.memory_exceeded


# --- flaky 재실행 -------------------------------------------------------------------

FAILED_REPORT: Final = (
    '{"summary": {"passed": 1, "failed": 1, "total": 2},'
    ' "tests": [{"nodeid": "tests/test_a.py::test_x", "outcome": "failed"}]}'
)


@pytest.fixture
def two_runs(monkeypatch, tmp_path: Path):
    """처음 실행은 실패 보고서를, 재실행은 rerun_outcome의 보고서를 남긴다."""
    calls: list[list[str]] = []

    def install(rerun_outcome: str | None, *, first=FAILED_REPORT, code: int = 1) -> list:
        def run(container, argv, **kwargs):
            calls.append(argv)
            if len(calls) == 1:
                (tmp_path / python.REPORT_FILE).write_text(first)
                return ExecResult(returncode=code, stdout="", stderr="")
            if rerun_outcome is not None:
                test = {"nodeid": "tests/test_a.py::test_x", "outcome": rerun_outcome}
                (tmp_path / python.RERUN_REPORT_FILE).write_text(json.dumps({"tests": [test]}))
            return ExecResult(
                returncode=0 if rerun_outcome == "passed" else 1, stdout="", stderr=""
            )

        (tmp_path / python.OUT_DIR).mkdir(exist_ok=True)
        monkeypatch.setattr(python, "run_in_sandbox", run)
        monkeypatch.setattr(docker_cli, "container_state", lambda name: RUNNING)
        return calls

    return install


def test_failed_test_passing_on_rerun_is_flaky(two_runs, tmp_path: Path) -> None:
    calls = two_runs("passed")
    run = python.run_tests("job-1", tmp_path)
    assert run.flaky == ("tests/test_a.py::test_x",) and run.status == "passed"
    assert calls[1][-2:] == ["--", "tests/test_a.py::test_x"]  # 실패한 테스트만
    assert not any(a.startswith("--cov") for a in calls[1])  # coverage는 처음 실행 것


def test_failed_again_stays_failed(two_runs, tmp_path: Path) -> None:
    two_runs("failed")
    run = python.run_tests("job-1", tmp_path)
    assert run.flaky == () and run.status == "failed"


def test_collection_errors_are_not_rerun(two_runs, tmp_path: Path) -> None:
    collector = {"nodeid": "tests/x.py", "outcome": "failed"}
    report = json.dumps({"summary": {"total": 0}, "collectors": [collector]})
    calls = two_runs("passed", first=report, code=2)
    python.run_tests("job-1", tmp_path)
    assert len(calls) == 1


def test_many_failures_are_not_rerun(two_runs, tmp_path: Path) -> None:
    n = python.MAX_RERUN_TESTS + 1
    tests = ", ".join(f'{{"nodeid": "t::{i}", "outcome": "failed"}}' for i in range(n))
    report = f'{{"summary": {{"failed": {n}, "total": {n}}}, "tests": [{tests}]}}'
    calls = two_runs("passed", first=report)
    python.run_tests("job-1", tmp_path)
    assert len(calls) == 1


def test_report_that_is_a_fifo_or_symlink_is_missing(tmp_path) -> None:
    """PR 코드가 결과 파일을 FIFO로 바꿔도 워커가 멈추지 않고, 링크를 따라가지 않는다."""
    import os

    from app.worker.lang.python import passed_ids, read_report

    fifo = tmp_path / "report.json"
    os.mkfifo(fifo)
    assert read_report(fifo) is None and passed_ids(fifo) == set()
    secret = tmp_path / "secret.json"
    secret.write_text('{"summary": {"total": 1, "passed": 1}, "tests": []}')
    link = tmp_path / "link.json"
    link.symlink_to(secret)
    assert read_report(link) is None
