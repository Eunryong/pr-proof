"""docker CLI 호출과 공통 옵션, 베이스 이미지.

호스트에서는 docker CLI만 부른다. subprocess에는 최소 환경만 넘긴다.
"""

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

IMAGE: Final = "pipeline-sandbox"
# 도구 설치와 검사기 실행 컨테이너의 이미지 (docs/DESIGN.md 2.3, 8.1). 대상 리포의 것을 넣지 않는다.
# glibc(도구 캐시의 gnu 빌드), 인증서(설치 컨테이너의 https), Python(받기 스크립트)이 든다
TOOL_IMAGE: Final = "python:3.11-slim"
# 작업 컨테이너 이름 접두어. 이름 뒤에는 runs ID가 붙는다
CONTAINER_PREFIX: Final = "pr-proof-job"
# 베이스 이미지 Dockerfile과 컨테이너에 읽기 전용으로 넣는 파이프라인 설정(coveragerc)
IMAGE_DIR: Final = Path(__file__).resolve().parents[2] / "docker" / "sandbox"

# 설치·작업 컨테이너 공통 자원 제한. 메모리는 대상 리포의 sandbox.memory_gb로 바꿀 수 있다
MEMORY_LIMIT_GB: Final = 1.0
_FIXED_LIMITS: Final = ("--cpus", "1", "--pids-limit", "256")
# PR 코드에 리눅스 권한(capability)을 주지 않고, setuid 바이너리로도 권한을 올리지 못하게 한다.
# 호스트 UID로 실행하므로 /work·.venv 쓰기와 pip 설치에는 권한이 필요 없다
_HARDENING: Final = ("--cap-drop", "ALL", "--security-opt", "no-new-privileges")
# docker 자체가 실패한 종료 코드: 125 docker 오류, 126 실행 불가, 127 명령 없음
DOCKER_FAILED: Final = frozenset({125, 126, 127})

_ENV_PASSTHROUGH: Final = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG")


class SandboxError(RuntimeError):
    """Docker나 .venv 설치가 실패한 환경 오류. PR의 테스트 실패와 구분한다."""


@dataclass(frozen=True)
class Image:
    """샌드박스 베이스 이미지. .venv 캐시 키에 들어간다."""

    id: str
    python_version: str


def limits(memory_gb: float = MEMORY_LIMIT_GB) -> tuple[str, ...]:
    """설치·작업 컨테이너 공통의 자원 제한과 권한 제한 인자.

    메모리는 MB 정수로 넘긴다 (1.5GB 같은 소수 한도).
    """
    return ("--memory", f"{int(memory_gb * 1024)}m", *_FIXED_LIMITS, *_HARDENING)


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


DEFAULT_NAMES: Final = Names()  # 운영 기본값


def ensure_image(tag: str = IMAGE) -> Image:
    """베이스 이미지를 빌드(캐시되면 즉시 끝남)하고 id와 Python 버전을 돌려준다."""
    run("build", "-q", "-t", tag, str(IMAGE_DIR), timeout=600)
    info = json.loads(run("image", "inspect", tag, "--format", "{{json .}}").stdout)
    env = dict(item.split("=", 1) for item in info["Config"]["Env"] if "=" in item)
    return Image(id=info["Id"], python_version=env.get("PYTHON_VERSION", "unknown"))


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
