import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest
from redis import Redis, RedisError

EVAL_REPO: Final = "Eunryong/eval"
TEST_REDIS_URL_ENV: Final = "PR_PROOF_TEST_REDIS_URL"
DEFAULT_TEST_REDIS_URL: Final = "redis://127.0.0.1:6379/15"
# 운영 서버(0번)와 다른 서버(1번)가 쓰는 DB. 테스트는 여기에 붙지 않는다
RESERVED_REDIS_DBS: Final = frozenset({0, 1})


@pytest.fixture(scope="session")
def test_redis_url() -> str:
    """Redis의 동작을 보는 테스트가 쓸 주소. 붙을 수 없거나 쓰면 안 되는 DB면 건너뛴다.
    세션에서 처음 한 번만 확인한다."""
    url = os.environ.get(TEST_REDIS_URL_ENV) or DEFAULT_TEST_REDIS_URL
    try:
        client = Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
    except ValueError as e:
        pytest.skip(f"{TEST_REDIS_URL_ENV}의 주소({url})를 읽지 못해 건너뜀: {e}")
    db = int(client.connection_pool.connection_kwargs.get("db") or 0)  # 주소에 없으면 0번
    if db in RESERVED_REDIS_DBS:
        pytest.skip(f"테스트용 Redis 주소({url})가 DB {db}번을 가리켜 건너뜀. 0번과 1번은 운영과 "
                    f"다른 서버가 쓴다. {TEST_REDIS_URL_ENV}로 다른 DB를 주세요")  # fmt: skip
    try:
        keys = client.dbsize()
    except RedisError as e:
        pytest.skip(f"테스트용 Redis({url})에 붙지 못해 건너뜀: {e}")
    finally:
        client.close()
    if keys:
        pytest.skip(f"테스트용 Redis DB({url})에 키가 이미 {keys}개 있어 지우지 않고 건너뜀. "
                    f"빈 DB를 {TEST_REDIS_URL_ENV}로 주세요")  # fmt: skip
    return url


@pytest.fixture
def real_redis(test_redis_url: str) -> Iterator[Redis]:
    """실제 Redis. 테스트가 끝나면 그 DB만 비운다(flushdb)."""
    client = Redis.from_url(test_redis_url)
    try:
        yield client
    finally:
        client.flushdb()
        client.close()


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


@pytest.fixture(autouse=True)
def isolated_config_dir(tmp_path_factory: pytest.TempPathFactory, monkeypatch) -> None:
    """어느 테스트도 운영자의 ~/.config/pipeline(비밀값)을 읽지 않게 한다. 설정 폴더가
    필요한 테스트는 이 위에 자기 폴더를 다시 정한다."""
    from app.shared import config

    monkeypatch.setenv(config.CONFIG_DIR_ENV, str(tmp_path_factory.mktemp("no-config")))


@pytest.fixture(autouse=True)
def notifications(monkeypatch) -> list[str]:
    """어느 테스트도 이 Mac에 실제 알림을 띄우지 않는다. 띄웠을 글을 모은다. 실제로 띄우는 것은
    `PR_PROOF_REAL_NOTIFY=1`일 때의 tests/shared/test_notify_real.py뿐이다."""
    from app.shared import notify as macos

    shown: list[str] = []
    monkeypatch.setattr(macos, "show_notification", lambda text: shown.append(text) or True)
    return shown


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


@pytest.fixture(scope="session")
def sandbox_image() -> str:
    """테스트용 태그로 빌드한 샌드박스 이미지. 받기 컨테이너도 이 이미지의 curl을 쓴다."""
    from app.worker.sandbox import docker_cli
    from tests.sandbox_names import TEST_NAMES

    docker_cli.ensure_image(TEST_NAMES.image)
    return TEST_NAMES.image
