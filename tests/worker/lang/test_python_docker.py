"""샌드박스 통합 테스트. 실제 Docker로 예제 리포(Eunryong/eval)의 테스트를 돌린다."""

import os
import subprocess
from pathlib import Path

import pytest

from app.sandbox import container, docker_cli
from app.sandbox.docker_cli import Image, SandboxError
from app.worker.lang import python
from app.worker.lang.python import DependencyInstallError
from tests.sandbox_names import TEST_NAMES

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def image() -> Image:
    return docker_cli.ensure_image(TEST_NAMES.image)


@pytest.fixture
def job(worktree: Path, image: Image, venv_cache: Path):
    """eval 리포를 마운트한 작업 컨테이너 이름."""
    venv_dir = python.ensure_venv(worktree, image, cache_dir=venv_cache, tag=TEST_NAMES.image)
    with container.running(
        worktree, [(venv_dir, python.VENV_IN_CONTAINER)], names=TEST_NAMES
    ) as name:
        yield name


def _exists(name: str) -> bool:
    return subprocess.run(["docker", "inspect", name], capture_output=True).returncode == 0


def test_passing_tests_leave_report_and_relative_coverage(job: str, worktree: Path) -> None:
    run = python.run_tests(job, worktree)
    assert run.ok and (run.summary.passed, run.summary.failed, run.summary.total) == (9, 0, 9)
    assert run.report_path == worktree / ".pipeline-out" / "report.json"
    xml = run.coverage_xml.read_text()
    assert 'filename="orders/services/pricing.py"' in xml
    assert "/work/" not in xml
    assert not list(worktree.rglob("__pycache__"))


def test_file_no_test_imports_is_in_coverage_as_zero(job: str, worktree: Path) -> None:
    """__init__.py 없는 폴더의 import 안 된 파일도 coverage.xml에 0%로 있어야 분모가 맞다."""
    tools = worktree / "scripts" / "tools"
    tools.mkdir(parents=True)
    (tools / "unused.py").write_text("x = 1\nprint(x)\n")
    run = python.run_tests(job, worktree)
    xml = run.coverage_xml.read_text()
    assert 'filename="scripts/tools/unused.py" complexity="0" line-rate="0"' in xml, xml


def test_failing_test_is_reported(job: str, worktree: Path) -> None:
    pricing = worktree / "orders" / "services" / "pricing.py"
    pricing.write_text(
        pricing.read_text().replace("FREE_SHIPPING_MIN = 50_000", "FREE_SHIPPING_MIN = 1")
    )
    run = python.run_tests(job, worktree)
    assert run.status == "failed" and run.summary.failed >= 1
    assert any(t.startswith("tests/test_pricing.py::") for t in run.summary.failed_tests)


def test_collection_error_is_reported(job: str, worktree: Path) -> None:
    (worktree / "tests" / "test_broken.py").write_text("import no_such_module\n")
    run = python.run_tests(job, worktree)
    assert run.status == "failed" and run.summary.errors >= 1
    assert "tests/test_broken.py" in run.summary.failed_tests


def test_no_network(job: str) -> None:
    code = "import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)"
    r = container.run_in_sandbox(job, [".venv/bin/python", "-c", code], timeout=10)
    assert r.returncode != 0


def test_venv_is_read_only(job: str) -> None:
    r = container.run_in_sandbox(job, ["touch", ".venv/injected"], timeout=10)
    assert r.returncode != 0


def test_runs_as_host_user(job: str) -> None:
    r = container.run_in_sandbox(job, ["id", "-u"], timeout=10)
    assert r.stdout.strip() == str(os.getuid())


def test_stale_commit_removes_container(job: str) -> None:
    r = container.run_in_sandbox(job, ["sleep", "60"], is_stale=lambda: True, poll_seconds=0.5)
    assert r.cancelled and not _exists(job)


def test_timeout_inside_container(job: str) -> None:
    r = container.run_in_sandbox(job, ["sleep", "30"], timeout=1, poll_seconds=0.5)
    assert r.timed_out


def test_container_removed_after_block(worktree: Path, image: Image, venv_cache: Path) -> None:
    venv_dir = python.ensure_venv(worktree, image, cache_dir=venv_cache, tag=TEST_NAMES.image)
    with container.running(
        worktree, [(venv_dir, python.VENV_IN_CONTAINER)], names=TEST_NAMES, job_id="7"
    ) as name:
        assert name == f"{TEST_NAMES.container_prefix}-7"  # 운영 컨테이너 이름과 겹치지 않는다
        assert _exists(name)
    assert not _exists(name)


def test_unknown_package_is_resolution_failure(
    worktree: Path, image: Image, tmp_path: Path
) -> None:
    """실제 pip 출력으로 분류를 확인한다. 없는 패키지는 1회에 PR 문제(resolution)다."""
    (worktree / "requirements.txt").write_text("pr-proof-no-such-package-xyz==0.0.1\n")
    cache = tmp_path / "cache"
    with pytest.raises(DependencyInstallError) as info:
        python.ensure_venv(worktree, image, cache_dir=cache, tag=TEST_NAMES.image)
    assert info.value.kind == "resolution"
    assert list(cache.iterdir()) == []


def test_source_only_package_is_not_built(worktree: Path, image: Image, venv_cache: Path) -> None:
    """소스 배포본만 있는 패키지는 빌드하지 않고 실패한다 (8.1). docopt는 wheel이 없다."""
    (worktree / "requirements.txt").write_text("pytest==8.3.4\ndocopt==0.6.2\n")
    with pytest.raises(DependencyInstallError) as info:
        python.ensure_venv(worktree, image, cache_dir=venv_cache, tag=TEST_NAMES.image)
    assert (info.value.kind, info.value.packages) == ("source_only", ("docopt",))
    assert "No matching distribution found for docopt" in info.value.detail


def test_broken_conftest_is_unrunnable_not_environment_error(job: str, worktree: Path) -> None:
    (worktree / "conftest.py").write_text("raise RuntimeError('broken conftest')\n")
    run = python.run_tests(job, worktree)
    assert run.status == "unrunnable"
    assert "broken conftest" in run.output


def test_container_gone_is_environment_error(job: str, worktree: Path) -> None:
    docker_cli.remove_container(job)
    with pytest.raises(SandboxError):
        python.run_tests(job, worktree)


def test_memory_limit_is_unrunnable_not_environment_error(job: str, worktree: Path) -> None:
    # 페이지를 실제로 쓰도록 b"x" * n을 쓴다 (bytearray(n)은 0 페이지라 메모리가 늘지 않음)
    hog = 'def test_hog():\n    data = b"x" * (3 * 1024 ** 3)\n    assert data\n'
    (worktree / "tests" / "test_hog.py").write_text(hog)
    run = python.run_tests(job, worktree)
    assert run.status == "unrunnable" and run.memory_exceeded


def test_flaky_test_passes_on_rerun(job: str, worktree: Path) -> None:
    """처음엔 실패하고 두 번째엔 통과하는 테스트. 표시 파일은 컨테이너의 /tmp에 둔다."""
    (worktree / "tests" / "test_flaky.py").write_text(
        "from pathlib import Path\n\n\n"
        "def test_sometimes():\n"
        "    marker = Path('/tmp/flaky-marker')\n"
        "    if not marker.exists():\n"
        "        marker.write_text('x')\n"
        "        raise AssertionError('first run fails')\n"
    )
    run = python.run_tests(job, worktree)
    assert run.flaky == ("tests/test_flaky.py::test_sometimes",)
    assert run.status == "passed" and not run.allows_code_fix and run.allows_mechanical_fix


def test_container_has_no_capabilities(job: str) -> None:
    """--cap-drop ALL과 no-new-privileges가 실제 컨테이너에 걸린다."""
    status = subprocess.run(
        ["docker", "exec", job, "cat", "/proc/self/status"], capture_output=True, text=True
    ).stdout
    fields = dict(line.split(":\t", 1) for line in status.splitlines() if ":\t" in line)
    assert int(fields["CapEff"].strip(), 16) == 0
    assert int(fields["CapBnd"].strip(), 16) == 0  # setuid 바이너리로도 얻지 못한다
    assert fields["NoNewPrivs"].strip() == "1"
