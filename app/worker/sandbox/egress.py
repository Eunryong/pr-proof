"""굽는 컨테이너의 네트워크: 내부 네트워크와 바깥 연결 프록시 (DESIGN 8.1, 위협 T41).

굽기마다 `docker network create --internal`로 네트워크를 새로 만들고, 기본 bridge에 띄운 프록시
컨테이너(`egress_proxy.py`)를 그 네트워크에도 붙인다. 굽는 컨테이너는 내부 네트워크에만 붙어
프록시로만 밖에 닿는다. 프록시는 이름을 풀어 공용 주소에만 연결한다(`egress_pure.public_addresses`).
네트워크와 프록시에는 기한 라벨을 붙인다. 워커가 죽어 남은 것은 다음 굽기가 기한을 보고 지운다.
"""

import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.worker.sandbox import docker_cli
from app.worker.sandbox.docker_cli import SandboxError
from app.worker.sandbox.egress_pure import LOG_PATH, PORT, UNTIL_LABEL, Event, expired, parse_event

PROXY_DIR: Final = docker_cli.IMAGE_DIR.parent / "proxy"
# 프록시 이미지가 `COPY --from=src`로 읽는 폴더. egress_proxy.py와 egress_pure.py가 있다
SOURCE_DIR: Final = Path(__file__).resolve().parent
PROXY_MEMORY_GB: Final = 0.25
PROXY_USER: Final = "65534:65534"  # nobody
_READY_TIMEOUT_S: Final = 30
_READY_POLL_S: Final = 0.1


@dataclass(frozen=True)
class Egress:
    """굽기 하나의 내부 네트워크와 프록시 컨테이너 이름."""

    network: str
    proxy: str

    def env(self) -> dict[str, str]:
        """굽는 명령에 넘기는 프록시 변수. 이미지에 커밋되지 않게 `docker exec`마다 넘긴다."""
        url = f"http://{self.proxy}:{PORT}"
        proxies = {k: url for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")}
        return proxies | {"NO_PROXY": "", "no_proxy": ""}

    def events(self) -> tuple[Event, ...]:
        """프록시가 지금까지 받은 요청. 프록시가 멈췄으면 환경 오류다."""
        proc = docker_cli.run("exec", self.proxy, "cat", LOG_PATH, timeout=60, check=False)
        if proc.returncode != 0:
            why = f"({proc.returncode}): {proc.stderr[-300:]}"
            raise SandboxError(f"프록시의 기록을 읽지 못함 {why}")
        return tuple(e for line in proc.stdout.splitlines() if (e := parse_event(line)) is not None)


@contextmanager
def opened(
    names: docker_cli.Names, lifetime_s: float, *, now: Callable[[], float] = time.time
) -> Iterator[Egress]:
    """내부 네트워크와 프록시를 띄운다. 블록을 나가면(실패, 취소 포함) 둘 다 지운다.

    lifetime_s는 굽기 하나가 끝나는 가장 늦은 때까지의 초다. 그 뒤로는 다른 프로세스가 지워도 된다.
    """
    sweep(now())
    docker_cli.ensure_image(names.proxy_image, context=PROXY_DIR, contexts=(("src", SOURCE_DIR),))
    session = uuid.uuid4().hex[:12]
    prefix = names.container_prefix
    egress = Egress(network=f"{prefix}-egress-{session}", proxy=f"{prefix}-proxy-{session}")
    label = ("--label", f"{UNTIL_LABEL}={int(now() + lifetime_s)}")
    try:
        docker_cli.run("network", "create", "--internal", *label, egress.network)
        docker_cli.run(
            "run", "-d", "--name", egress.proxy, *label, "--network", "bridge",
            "--read-only", "--tmpfs", "/tmp:size=64m", "--user", PROXY_USER,
            *docker_cli.limits(PROXY_MEMORY_GB), names.proxy_image,
        )  # fmt: skip
        docker_cli.run("network", "connect", egress.network, egress.proxy)
        _wait_ready(egress.proxy)
        yield egress
    finally:
        close(egress.network, egress.proxy)


def close(network: str, *containers: str) -> None:
    """네트워크에 붙은 컨테이너와 containers를 지우고 네트워크를 지운다. 이미 없어도 된다."""
    attached = docker_cli.run(
        "network", "inspect", network, "--format",
        "{{range .Containers}}{{.Name}} {{end}}", check=False,
    ).stdout.split()  # fmt: skip
    for name in {*containers, *attached}:
        docker_cli.remove_container(name)
    docker_cli.run("network", "rm", network, check=False)


def sweep(now: float) -> None:
    """기한이 지난 프록시와 내부 네트워크를 지운다. 죽은 워커가 남긴 것이다."""
    label = f'{{{{.Label "{UNTIL_LABEL}"}}}}'
    containers = docker_cli.run(
        "ps", "-a", "--filter", f"label={UNTIL_LABEL}", "--format", "{{.Names}}\t" + label
    ).stdout
    for name in expired(containers, now):
        docker_cli.remove_container(name)
    networks = docker_cli.run(
        "network", "ls", "--filter", f"label={UNTIL_LABEL}", "--format", "{{.Name}}\t" + label
    ).stdout
    for name in expired(networks, now):
        close(name)


def _wait_ready(proxy: str) -> None:
    deadline = time.monotonic() + _READY_TIMEOUT_S
    while time.monotonic() < deadline:
        if docker_cli.run("exec", proxy, "test", "-e", LOG_PATH, check=False).returncode == 0:
            return
        state = docker_cli.container_state(proxy)
        if state is None or not state.running:
            break
        time.sleep(_READY_POLL_S)
    logs = docker_cli.run("logs", "--tail", "20", proxy, check=False)
    raise SandboxError(f"프록시가 준비되지 않음: {(logs.stdout + logs.stderr)[-500:]}")
