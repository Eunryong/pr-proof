"""샌드박스 통합 테스트. 실제 Docker로 예제 리포(Eunryong/eval)의 테스트를 돌린다."""

import os
import subprocess
from pathlib import Path

import pytest

from app.models.sandbox import Image
from app.services import sandbox as sb

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def image() -> Image:
    return sb.ensure_image()


@pytest.fixture
def container(worktree: Path, image: Image, venv_cache: Path):
    venv = sb.ensure_venv(worktree, image, cache_dir=venv_cache)
    with sb.sandbox(worktree, venv) as name:
        yield name


def _exists(name: str) -> bool:
    return subprocess.run(["docker", "inspect", name], capture_output=True).returncode == 0


def test_passing_tests_leave_report_and_relative_coverage(container: str, worktree: Path) -> None:
    run = sb.run_tests(container, worktree)
    assert run.ok and (run.summary.passed, run.summary.failed, run.summary.total) == (9, 0, 9)
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
    assert run.status == "failed" and run.summary.failed >= 1
    assert any(t.startswith("tests/test_pricing.py::") for t in run.summary.failed_tests)


def test_collection_error_is_reported(container: str, worktree: Path) -> None:
    (worktree / "tests" / "test_broken.py").write_text("import no_such_module\n")
    run = sb.run_tests(container, worktree)
    assert run.status == "failed" and run.summary.errors >= 1
    assert "tests/test_broken.py" in run.summary.failed_tests


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


def test_container_removed_after_block(worktree: Path, image: Image, venv_cache: Path) -> None:
    venv = sb.ensure_venv(worktree, image, cache_dir=venv_cache)
    with sb.sandbox(worktree, venv) as name:
        assert _exists(name)
    assert not _exists(name)


def test_install_failure_is_environment_error(worktree: Path, image: Image, tmp_path: Path) -> None:
    (worktree / "requirements.txt").write_text("pr-proof-no-such-package-xyz==0.0.1\n")
    cache = tmp_path / "cache"
    with pytest.raises(sb.SandboxError):
        sb.ensure_venv(worktree, image, cache_dir=cache)
    assert list(cache.iterdir()) == []
