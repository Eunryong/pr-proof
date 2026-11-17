"""샌드박스 통합 테스트. 실제 Docker로 예제 리포(Eunryong/eval)의 테스트를 돌린다."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app import sandbox as sb

pytestmark = pytest.mark.docker

EVAL_REPO = "Eunryong/eval"


@pytest.fixture(scope="module")
def eval_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """예제 리포를 한 번 클론한다. gh 로그인이 없거나 네트워크가 막히면 건너뛴다."""
    dest = tmp_path_factory.mktemp("eval") / "repo"
    try:
        proc = subprocess.run(
            ["gh", "repo", "clone", EVAL_REPO, str(dest), "--", "--depth", "1", "-q"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        pytest.skip(f"{EVAL_REPO} 클론 실패: {e}")
    if proc.returncode != 0:
        pytest.skip(f"{EVAL_REPO} 클론 실패: {proc.stderr.strip()}")
    return dest


@pytest.fixture(scope="module")
def image() -> sb.Image:
    return sb.ensure_image()


@pytest.fixture(scope="module")
def venv_cache(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("venvs")


@pytest.fixture
def worktree(eval_repo: Path, tmp_path: Path) -> Path:
    """테스트마다 클론을 복사해 쓴다 (2단계의 worktree 자리)."""
    dest = tmp_path / "wt"
    shutil.copytree(eval_repo, dest, ignore=shutil.ignore_patterns(".git"))
    return dest


@pytest.fixture
def container(worktree: Path, image: sb.Image, venv_cache: Path):
    venv = sb.ensure_venv(worktree, image, cache_dir=venv_cache)
    with sb.sandbox(worktree, venv) as name:
        yield name


def _exists(name: str) -> bool:
    return subprocess.run(["docker", "inspect", name], capture_output=True).returncode == 0


def test_passing_tests_leave_report_and_relative_coverage(container: str, worktree: Path) -> None:
    run = sb.run_tests(container, worktree)
    assert run.ok and (run.passed, run.failed, run.total) == (9, 0, 9)
    assert run.report_path == worktree / ".pipeline-out" / "report.json"
    xml = run.coverage_xml.read_text()
    assert 'filename="orders/services/pricing.py"' in xml
    assert "/work/" not in xml
    assert not list(worktree.rglob("__pycache__"))


def test_failing_test_is_reported(container: str, worktree: Path) -> None:
    pricing = worktree / "orders" / "services" / "pricing.py"
    pricing.write_text(
        pricing.read_text().replace("FREE_SHIPPING_MIN = 50_000", "FREE_SHIPPING_MIN = 1")
    )
    run = sb.run_tests(container, worktree)
    assert run.exit_code == 1 and run.failed >= 1
    assert any(t.startswith("tests/test_pricing.py::") for t in run.failed_tests)


def test_collection_error_is_reported(container: str, worktree: Path) -> None:
    (worktree / "tests" / "test_broken.py").write_text("import no_such_module\n")
    run = sb.run_tests(container, worktree)
    assert not run.ok and run.errors >= 1
    assert "tests/test_broken.py" in run.failed_tests


def test_no_network(container: str) -> None:
    code = "import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)"
    r = sb.run_in_sandbox(container, [".venv/bin/python", "-c", code], timeout=10)
    assert r.returncode != 0


def test_venv_is_read_only(container: str) -> None:
    r = sb.run_in_sandbox(container, ["touch", ".venv/injected"], timeout=10)
    assert r.returncode != 0


def test_runs_as_host_user(container: str) -> None:
    r = sb.run_in_sandbox(container, ["id", "-u"], timeout=10)
    assert r.stdout.strip() == str(os.getuid())


def test_stale_commit_removes_container(container: str) -> None:
    r = sb.run_in_sandbox(container, ["sleep", "60"], is_stale=lambda: True, poll_seconds=0.5)
    assert r.cancelled and not _exists(container)


def test_timeout_inside_container(container: str) -> None:
    r = sb.run_in_sandbox(container, ["sleep", "30"], timeout=1, poll_seconds=0.5)
    assert r.timed_out


def test_container_removed_after_block(worktree: Path, image: sb.Image, venv_cache: Path) -> None:
    venv = sb.ensure_venv(worktree, image, cache_dir=venv_cache)
    with sb.sandbox(worktree, venv) as name:
        assert _exists(name)
    assert not _exists(name)


def test_install_failure_is_environment_error(
    worktree: Path, image: sb.Image, tmp_path: Path
) -> None:
    (worktree / "requirements.txt").write_text("pr-proof-no-such-package-xyz==0.0.1\n")
    cache = tmp_path / "cache"
    with pytest.raises(sb.SandboxError):
        sb.ensure_venv(worktree, image, cache_dir=cache)
    assert list(cache.iterdir()) == []
