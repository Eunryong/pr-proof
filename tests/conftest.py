import shutil
import subprocess
from pathlib import Path
from typing import Final

import pytest

EVAL_REPO: Final = "Eunryong/eval"


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Docker가 없으면 docker 테스트를 건너뛴다."""
    docker_items = [item for item in items if "docker" in item.keywords]
    if not docker_items or _docker_available():
        return
    skip = pytest.mark.skip(reason="Docker를 쓸 수 없다")
    for item in docker_items:
        item.add_marker(skip)


@pytest.fixture(scope="session")
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


@pytest.fixture(scope="session")
def venv_cache(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("venvs")


@pytest.fixture
def worktree(eval_repo: Path, tmp_path: Path) -> Path:
    """테스트마다 클론을 복사해 쓴다 (worktree 생성이 들어오기 전 임시 자리)."""
    dest = tmp_path / "wt"
    shutil.copytree(eval_repo, dest, ignore=shutil.ignore_patterns(".git"))
    return dest


@pytest.fixture(scope="session")
def tool_cache(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """docker 테스트가 같이 쓰는 도구 캐시. 세션에 한 번 실제로 받는다."""
    return tmp_path_factory.mktemp("tools")
