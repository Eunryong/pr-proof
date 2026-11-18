"""작업 컨테이너 띄우기와 그 안에서 명령 실행 (docs/DESIGN.md 보안·인증 > 샌드박스).

작업당 컨테이너 하나를 띄우고 명령은 docker exec로 넣는다. pytest, mutmut, ruff가 모두
run_in_sandbox()를 거친다.
"""

import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.sandbox import docker_cli
from app.sandbox.venv import VENV_IN_CONTAINER

WORKDIR: Final = "/work"
# 파이프라인 설정(coveragerc 등)을 읽기 전용으로 넣는 경로
PIPELINE_DIR: Final = "/pipeline"
OUT_DIR: Final = ".pipeline-out"

DEFAULT_TIMEOUT: Final = 600
POLL_SECONDS: Final = 10.0
# 컨테이너 안 `timeout`이 이만큼 더 지나도 끝나지 않으면 호스트에서 컨테이너를 지운다
HOST_GRACE_SECONDS: Final = 30
TIMEOUT_EXIT: Final = 124  # coreutils `timeout`의 종료 코드


@dataclass(frozen=True)
class ExecResult:
    """컨테이너 안에서 실행한 명령 하나의 결과."""

    returncode: int | None  # 취소되면 None
    stdout: str
    stderr: str
    timed_out: bool = False
    cancelled: bool = False


@contextmanager
def running(
    worktree: Path,
    venv: Path | None,
    *,
    job_id: str | None = None,
    memory_gb: float = docker_cli.MEMORY_LIMIT_GB,
    names: docker_cli.Names = docker_cli.DEFAULT_NAMES,
) -> Iterator[str]:
    """작업 컨테이너를 띄우고 이름을 돌려준다. 블록을 나가면 지운다.

    네트워크 없음, 자원 제한, 호스트 UID·GID. .venv와 파이프라인 설정은 읽기 전용.
    venv가 None이면 .venv 없이 띄운다 (이미지에 든 ruff만 쓰는 규칙 검사).
    """
    name = f"{names.container_prefix}-{job_id or uuid.uuid4().hex[:12]}"
    (worktree / OUT_DIR).mkdir(exist_ok=True)
    venv_mount = ("-v", f"{venv.resolve()}:{VENV_IN_CONTAINER}:ro") if venv is not None else ()
    docker_cli.run(
        "run", "-d", "--name", name, "--network", "none", *docker_cli.limits(memory_gb),
        "--user", docker_cli.user(), "-e", "HOME=/tmp",
        "-v", f"{worktree.resolve()}:{WORKDIR}", "-w", WORKDIR,
        *venv_mount,
        "-v", f"{docker_cli.IMAGE_DIR}:{PIPELINE_DIR}:ro",
        names.image, "sleep", "infinity",
    )  # fmt: skip
    try:
        yield name
    finally:
        docker_cli.remove_container(name)


def run_in_sandbox(
    container: str,
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    is_stale: Callable[[], bool] | None = None,
    poll_seconds: float = POLL_SECONDS,
    host_grace: float = HOST_GRACE_SECONDS,
) -> ExecResult:
    """컨테이너 안에서 명령을 실행한다.

    끝날 때까지 poll_seconds마다 is_stale()을 확인해, 커밋이 낡았으면 컨테이너를 지우고
    cancelled로 돌려준다. 컨테이너 안의 `timeout`이 timeout + host_grace초 안에 끝내지
    못하면 호스트에서 지운다.
    """
    cmd = ["docker", "exec"]
    for k, v in (env or {}).items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [container, "timeout", str(timeout), *argv]

    cancelled = timed_out = False
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdout=out, stderr=err, env=docker_cli.minimal_env())
        deadline = time.monotonic() + timeout + host_grace
        while True:
            try:
                proc.wait(timeout=poll_seconds)
                break
            except subprocess.TimeoutExpired:
                pass
            if is_stale is not None and is_stale():
                cancelled = True
            elif time.monotonic() > deadline:
                timed_out = True
            else:
                continue
            docker_cli.remove_container(container)  # 컨테이너가 지워지면 docker exec도 끝난다
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            break
        out.seek(0)
        err.seek(0)
        stdout = out.read().decode(errors="replace")
        stderr = err.read().decode(errors="replace")

    if proc.returncode == TIMEOUT_EXIT and not cancelled:
        timed_out = True
    return ExecResult(
        returncode=None if cancelled else proc.returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        cancelled=cancelled,
    )
