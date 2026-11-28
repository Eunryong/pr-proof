"""docker CLI 호출과 공통 옵션, 베이스 이미지.

호스트에서는 docker CLI만 부른다. subprocess에는 최소 환경만 넘긴다.
"""

import logging
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

# 모든 샌드박스 컨테이너(도구 받기, .venv 설치, 검사기·ruff, 테스트)의 이미지. 언어와 무관한
# 최소 이미지이고 런타임과 도구는 도구 캐시에서 붙인다 (DESIGN 2.3, 8.1, docker/sandbox/Dockerfile)
IMAGE: Final = "pipeline-sandbox"
# 기반 이미지(debian:bookworm-slim)를 새로 받는 주기 (위협 T25)
PULL_EVERY_SECONDS: Final = 24 * 60 * 60
# 작업 컨테이너 이름 접두어. 이름 뒤에는 runs ID가 붙는다
CONTAINER_PREFIX: Final = "pr-proof-job"
# 이미지 Dockerfile과 컨테이너에 읽기 전용으로 넣는 파이프라인 설정(coveragerc)
IMAGE_DIR: Final = Path(__file__).resolve().parents[3] / "docker" / "sandbox"
# 굽는 컨테이너의 바깥 연결 프록시 이미지 (app/worker/sandbox/egress.py)
PROXY_IMAGE: Final = "pr-proof-proxy"

# 설치·작업 컨테이너 공통 자원 제한. 메모리는 대상 리포의 sandbox.memory_gb로 바꿀 수 있다
MEMORY_LIMIT_GB: Final = 1.0
_PIDS_LIMIT: Final = ("--pids-limit", "256")
# setuid 바이너리로 권한을 올리지 못하게 한다
NO_NEW_PRIVILEGES: Final = ("--security-opt", "no-new-privileges")
# PR 코드에 리눅스 권한(capability)을 주지 않는다.
# 호스트 UID로 실행하므로 /work·.venv 쓰기와 pip 설치에는 권한이 필요 없다
_HARDENING: Final = ("--cap-drop", "ALL", *NO_NEW_PRIVILEGES)
# docker 자체가 실패한 종료 코드: 125 docker 오류, 126 실행 불가, 127 명령 없음
DOCKER_FAILED: Final = frozenset({125, 126, 127})

_ENV_PASSTHROUGH: Final = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG")
# 이 프로세스가 태그마다 기반 이미지를 마지막으로 받은 시각 (time.monotonic)
_pulled_at: dict[str, float] = {}

log = logging.getLogger(__name__)


class SandboxError(RuntimeError):
    """Docker나 .venv 설치가 실패한 환경 오류. PR의 테스트 실패와 구분한다."""


@dataclass(frozen=True)
class Image:
    """샌드박스 이미지. id가 .venv 캐시 키에 들어간다 (glibc가 바뀌면 다시 설치한다)."""

    id: str


def resources(memory_gb: float = MEMORY_LIMIT_GB, cpus: float = 1) -> tuple[str, ...]:
    """메모리·CPU·프로세스 수 제한 인자. 메모리는 MB 정수로 넘긴다 (1.5GB 같은 소수 한도)."""
    return ("--memory", f"{int(memory_gb * 1024)}m", "--cpus", f"{cpus:g}", *_PIDS_LIMIT)


def limits(memory_gb: float = MEMORY_LIMIT_GB, cpus: float = 1) -> tuple[str, ...]:
    """설치·작업 컨테이너 공통의 자원 제한과 권한 제한 인자."""
    return (*resources(memory_gb, cpus), *_HARDENING)


def minimal_env() -> dict[str, str]:
    """docker CLI가 동작하는 데 필요한 변수만 넘긴다."""
    return {k: os.environ[k] for k in _ENV_PASSTHROUGH if k in os.environ}


def run(
    *args: str, timeout: float = 120, check: bool = True, input: str | None = None
) -> subprocess.CompletedProcess[str]:
    """`docker <args>`를 실행한다. check면 0이 아닌 종료 코드를 SandboxError로 올린다.

    input은 컨테이너의 표준 입력이다 (`docker run -i`와 같이 쓴다).
    """
    try:
        proc = subprocess.run(
            ["docker", *args],
            input=input,
            capture_output=True,
            text=True,
            env=minimal_env(),
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SandboxError(f"docker {args[0]} 실행 실패: {e}") from e
    if check and proc.returncode != 0:
        raise SandboxError(
            f"docker {args[0]} 실패 ({proc.returncode}): {proc.stderr.strip()[-2000:]}"
        )
    return proc


def user() -> str:
    """컨테이너를 호스트 사용자의 UID·GID로 실행한다 (.pipeline-out/ 쓰기 권한)."""
    return f"{os.getuid()}:{os.getgid()}"


@dataclass(frozen=True)
class Names:
    """이미지 태그와 작업 컨테이너 이름 접두어.

    테스트는 운영과 다른 값을 넘긴다. 같으면 테스트가 운영 이미지를 개발 중인 Dockerfile로 다시
    빌드하고, 같은 이름의 운영 컨테이너를 지울 수 있다.
    """

    image: str = IMAGE
    container_prefix: str = CONTAINER_PREFIX
    proxy_image: str = PROXY_IMAGE


DEFAULT_NAMES: Final = Names()  # 운영 기본값


def ensure_image(
    tag: str = IMAGE,
    *,
    context: Path = IMAGE_DIR,
    contexts: tuple[tuple[str, Path], ...] = (),
    now: Callable[[], float] = time.monotonic,
    pulled_at: dict[str, float] = _pulled_at,
) -> Image:
    """context 폴더의 Dockerfile로 이미지를 빌드(캐시되면 즉시 끝남)하고 id를 돌려준다.

    contexts는 Dockerfile이 `COPY --from=<이름>`으로 읽는 다른 폴더다(`--build-context`).
    기반 이미지의 보안 패치를 받으려고 프로세스가 처음 빌드할 때와 그 뒤 `PULL_EVERY_SECONDS`마다
    한 번 `--pull`로 빌드한다(위협 T25). 받기가 실패하면(네트워크) 있는 기반 이미지로 빌드하고
    다음 주기에 다시 받는다. pulled_at은 태그마다 마지막으로 받은 시각이고 테스트가 넘긴다.
    """
    named = [arg for name, path in contexts for arg in ("--build-context", f"{name}={path}")]
    build = ("build", "-q", *named, "-t", tag)
    last = pulled_at.get(tag)
    built = False
    if last is None or now() - last >= PULL_EVERY_SECONDS:
        pulled_at[tag] = now()
        try:
            run(*build, "--pull", str(context), timeout=600)
            built = True
        except SandboxError as e:
            log.warning("기반 이미지를 새로 받지 못해 있는 것으로 빌드함: %s", str(e)[-300:])
    if not built:
        run(*build, str(context), timeout=600)
    return Image(id=run("image", "inspect", tag, "--format", "{{.Id}}").stdout.strip())


@dataclass(frozen=True)
class ContainerState:
    running: bool
    oom_killed: bool  # 메모리 한도를 넘어 커널이 컨테이너 프로세스를 죽였는지


def container_state(name: str) -> ContainerState | None:
    """컨테이너 상태. 컨테이너가 없거나 docker에 닿지 않으면 None."""
    try:
        proc = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}} {{.State.OOMKilled}}", name],
            capture_output=True,
            text=True,
            env=minimal_env(),
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    running, _, oom = proc.stdout.strip().partition(" ")
    return ContainerState(running=running == "true", oom_killed=oom == "true")


def remove_container(name: str) -> None:
    """컨테이너를 지운다. 이미 없어도 오류로 보지 않는다."""
    subprocess.run(
        ["docker", "rm", "-f", name],
        capture_output=True,
        env=minimal_env(),
        timeout=60,
        check=False,
    )
