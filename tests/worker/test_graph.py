from pathlib import Path

import pytest

from app.worker.env.use_pure import NOT_PREPARED, EnvSkipped
from app.worker.graph import run_pr_tests
from app.worker.sandbox import docker_cli
from app.worker.sandbox.docker_cli import SandboxError
from tests.sandbox_names import TEST_NAMES


@pytest.mark.docker
def test_runs_eval_repo_tests(worktree: Path, venv_cache: Path) -> None:
    run = run_pr_tests(worktree, venv_cache=venv_cache, names=TEST_NAMES)
    assert run.status == "passed"
    assert run.report.total == 9
    assert run.coverage_xml is not None


def test_environment_error_propagates(tmp_path: Path, monkeypatch) -> None:
    def no_docker(*args, **kwargs):
        raise SandboxError("docker 없음")

    monkeypatch.setattr(docker_cli, "ensure_image", no_docker)
    (tmp_path / "a.py").write_text("")
    with pytest.raises(SandboxError):
        run_pr_tests(tmp_path, venv_cache=tmp_path / "cache")


def test_a_repo_without_python_runs_only_through_a_recipe(tmp_path: Path, monkeypatch) -> None:
    """Python 프로필이 돌릴 것이 없으면 Python 런타임을 준비하지 않는다. 환경 준비가 처방을 정하지
    않은 작업이면 그 까닭으로, 처방이 없다고 정했으면 그 까닭으로 `no_env`다."""
    monkeypatch.setattr(
        docker_cli, "ensure_image", lambda *a, **k: pytest.fail("docker를 부르면 안 된다")
    )
    (tmp_path / "engine").mkdir()
    (tmp_path / "engine" / "version.cpp").write_text("int main() {}")
    (tmp_path / "CMakeLists.txt").write_text("")
    run = run_pr_tests(tmp_path, venv_cache=tmp_path / "cache")
    assert (run.status, run.unplanned, run.report) == ("no_env", NOT_PREPARED, None)
    skipped = run_pr_tests(tmp_path, env=EnvSkipped("모델이 돌릴 것이 없다고 답함"))
    assert (skipped.status, skipped.unplanned) == ("no_env", "모델이 돌릴 것이 없다고 답함")
    assert "Python뿐" not in run.unplanned + skipped.unplanned
