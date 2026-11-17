from pathlib import Path

import pytest

from app.controllers.test_run import run_pr_tests
from app.services.sandbox import SandboxError


@pytest.mark.docker
def test_runs_eval_repo_tests(worktree: Path, venv_cache: Path) -> None:
    run = run_pr_tests(worktree, venv_cache=venv_cache)
    assert run.status == "passed"
    assert run.summary.total == 9
    assert run.coverage_xml is not None


def test_environment_error_propagates(tmp_path: Path, monkeypatch) -> None:
    from app.services import sandbox

    def no_docker(*args, **kwargs):
        raise SandboxError("docker 없음")

    monkeypatch.setattr(sandbox, "ensure_image", no_docker)
    with pytest.raises(SandboxError):
        run_pr_tests(tmp_path, venv_cache=tmp_path / "cache")
