"""app/worker/sandbox/container.py 단위 테스트. docker exec 대신 로컬 프로세스로 대기·취소를
확인한다."""

import subprocess

import pytest

from app.worker.sandbox import container, docker_cli


@pytest.fixture
def fake_exec(monkeypatch):
    """docker exec 자리에 로컬 명령을 띄우고, remove_container는 그 프로세스를 끝낸다."""
    state: dict = {"cmd": None, "procs": [], "removed": []}
    real_popen = subprocess.Popen

    def install(local_cmd: list[str]) -> dict:
        def popen(cmd, **kwargs):
            state["cmd"] = cmd
            proc = real_popen(local_cmd, **kwargs)
            state["procs"].append(proc)
            return proc

        def remove(name: str) -> None:
            state["removed"].append(name)
            for p in state["procs"]:
                p.terminate()

        monkeypatch.setattr(container.subprocess, "Popen", popen)
        monkeypatch.setattr(docker_cli, "remove_container", remove)
        return state

    return install


def test_run_in_sandbox_builds_exec_command(fake_exec) -> None:
    state = fake_exec(["sh", "-c", "echo out; echo err >&2"])
    r = container.run_in_sandbox(
        "job-1", [".venv/bin/pytest", "-q"], env={"PYTHONPATH": "/work"}, timeout=5
    )
    assert state["cmd"] == [
        "docker", "exec", "-e", "PYTHONPATH=/work", "job-1",
        "timeout", "5", ".venv/bin/pytest", "-q",
    ]  # fmt: skip
    assert (r.returncode, r.stdout, r.stderr) == (0, "out\n", "err\n")
    assert not r.cancelled and not r.timed_out and state["removed"] == []


def test_run_in_sandbox_returns_failure_code(fake_exec) -> None:
    fake_exec(["sh", "-c", "exit 1"])
    r = container.run_in_sandbox("job-1", ["x"])
    assert r.returncode == 1 and not r.timed_out


def test_run_in_sandbox_cancels_when_stale(fake_exec) -> None:
    state = fake_exec(["sleep", "30"])
    checks = iter([False, False, True])
    r = container.run_in_sandbox("job-1", ["x"], is_stale=lambda: next(checks), poll_seconds=0.05)
    assert r.cancelled and r.returncode is None
    assert state["removed"] == ["job-1"]


def test_run_in_sandbox_not_stale_runs_to_end(fake_exec) -> None:
    state = fake_exec(["sh", "-c", "sleep 0.3"])
    r = container.run_in_sandbox("job-1", ["x"], is_stale=lambda: False, poll_seconds=0.05)
    assert r.returncode == 0 and not r.cancelled and state["removed"] == []


def test_run_in_sandbox_timeout_exit_code(fake_exec) -> None:
    fake_exec(["sh", "-c", f"exit {container.TIMEOUT_EXIT}"])
    r = container.run_in_sandbox("job-1", ["x"])
    assert r.timed_out and r.returncode == container.TIMEOUT_EXIT


def test_run_in_sandbox_host_deadline_removes_container(fake_exec) -> None:
    state = fake_exec(["sleep", "30"])
    r = container.run_in_sandbox("job-1", ["x"], timeout=0, poll_seconds=0.05, host_grace=0)
    assert r.timed_out and not r.cancelled and state["removed"] == ["job-1"]
