from pathlib import Path

import pytest

from tests.sandbox_names import TEST_NAMES
from worker.graph import run_pr_tests
from worker.sandbox import docker_cli
from worker.sandbox.docker_cli import SandboxError


@pytest.mark.docker
def test_runs_eval_repo_tests(worktree: Path, venv_cache: Path) -> None:
    run = run_pr_tests(worktree, venv_cache=venv_cache, names=TEST_NAMES)
    assert run.status == "passed"
    assert run.summary.total == 9
    assert run.coverage_xml is not None


def test_environment_error_propagates(tmp_path: Path, monkeypatch) -> None:
    def no_docker(*args, **kwargs):
        raise SandboxError("docker 없음")

    monkeypatch.setattr(docker_cli, "ensure_image", no_docker)
    with pytest.raises(SandboxError):
        run_pr_tests(tmp_path, venv_cache=tmp_path / "cache")
