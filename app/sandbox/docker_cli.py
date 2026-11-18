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
# 베이스 이미지 Dockerfile과 컨테이너에 읽기 전용으로 넣는 파이프라인 설정(coveragerc)
IMAGE_DIR: Final = Path(__file__).resolve().parents[2] / "docker" / "sandbox"

# 설치·작업 컨테이너 공통 자원 제한. 메모리는 PR 코멘트 안내에도 쓴다
MEMORY_LIMIT_GB: Final = 1
LIMITS: Final = ("--memory", f"{MEMORY_LIMIT_GB}g", "--cpus", "1", "--pids-limit", "256")
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


def minimal_env() -> dict[str, str]:
    """docker CLI가 동작하는 데 필요한 변수만 넘긴다."""
    return {k: os.environ[k] for k in _ENV_PASSTHROUGH if k in os.environ}


def run(*args: str, timeout: float = 120, check: bool = True) -> subprocess.CompletedProcess[str]:
    """`docker <args>`를 실행한다. check면 0이 아닌 종료 코드를 SandboxError로 올린다."""
    try:
        proc = subprocess.run(
            ["docker", *args],
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
