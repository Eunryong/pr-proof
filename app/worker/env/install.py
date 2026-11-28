"""처방의 두 층(시스템, 의존성)을 Docker 이미지로 굽고 캐시한다 (DESIGN 7.10, 8.1).

`docker build`가 아니라 `docker run` + `docker commit`이다. 설치 컨테이너에 자원 제한, 권한 제한,
시간 상한을 걸고 호스트 경로를 붙이지 않기 위해서다. 층 이미지는 `pr-proof-env:<키>`이고 키는
내용에서 나온다(`recipe_pure.system_key`, `deps_key`). 태그가 있으면 다시 굽지 않는다. 같은 키를
둘이 같이 구우면 둘 다 같은 내용을 커밋하고 마지막 태그가 남는다. 실패한 층은 커밋하지 않는다.
굽는 컨테이너는 내부 네트워크에만 붙고, 공용 주소에만 연결하는 프록시로 밖에 닿는다
(`sandbox/egress.py`, 위협 T41).
"""

import json
import logging
import shutil
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Final

from app.worker.env import install_pure
from app.worker.env.install_pure import BakeFailed, LayerName
from app.worker.env.recipe_pure import (
    ENV_ROOT,
    OVERLAY_DIR,
    SRC_DIR,
    EnvRecipe,
    deps_key,
    system_key,
)
from app.worker.sandbox import docker_cli, egress
from app.worker.sandbox.container import run_in_sandbox
from app.worker.sandbox.docker_cli import SandboxError
from app.worker.sandbox.egress import Egress
from app.worker.sandbox.egress_pure import Event
from app.worker.sandbox.safe_read import read_regular_file

ENV_IMAGE: Final = "pr-proof-env"
ENV_LABEL: Final = "pr-proof.env=1"
# 시스템 층(root)에 남기는 권한. 도커 기본 권한에서 하나씩 빼 보며 apt-get install이 도는 가장 작은
# 묶음을 찾았다. SETUID·SETGID는 apt가 받기를 _apt 사용자로 내릴 때, DAC_OVERRIDE는 _apt가 가진
# 폴더를 root가 열 때, CHOWN은 dpkg와 /opt/env를 호스트 UID에게 줄 때 쓴다. FOWNER가 없으면 apt가
# 경고를 내고, root의 tar가 다른 주인의 파일을 풀 때 권한을 맞추지 못해 실패한다(종료 코드 2)
SYSTEM_CAPS: Final = ("CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID")

# 상한의 기본값은 DemoCue에서 잰 값에서 정했다. 의존성 설치(vcpkg가 OpenCV를 소스에서 빌드)는
# CPU 1, 메모리 4GB에서 984초가 걸렸고 메모리는 가장 많을 때 3GB였다. 예전 설치 컨테이너의
# 900초·1GB·CPU 1로는 끝나지 않는다. 설치 컨테이너는 작업마다 하나라 호스트 CPU의 절반을 준다
MEMORY_GB: Final = 4.0
INSTALL_TIMEOUT_S: Final = 3600  # 층 하나의 명령 전부에 거는 상한. 984초에 여유를 둔 첫 값
STEP_TIMEOUT_S: Final = 600  # 실행 단계 하나. DemoCue에서 가장 긴 것이 빌드 19초
MAX_INPUT_BYTES: Final = 16 * 1024 * 1024  # deps.inputs 파일 하나 (잠금 파일도 들어가게)
MAX_OUTPUT_CHARS: Final = 4000
_PROBE_TIMEOUT_S: Final = 60
_PULL_TIMEOUT_S: Final = 1800
_COMMIT_TIMEOUT_S: Final = 1800
# 프록시와 내부 네트워크의 기한에 층 하나의 상한 위로 더하는 초(받기 확인, 복사, 컨테이너 띄우기)
_EGRESS_SLACK_S: Final = 600
# git이 없으면 처방이 먼저 설치해야 한다는 것을 알린다. 인자는 위치 인자로 넘겨 셸이 풀지 않는다
_FETCH_SCRIPT: Final = (
    'command -v git >/dev/null || { echo "git이 없음: system.commands에서 git을 먼저 설치해야 함"; '
    'exit 127; }; git clone --quiet -- "$1" "$2" && '
    'git -C "$2" -c advice.detachedHead=false checkout --quiet "$3"'
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Limits:
    """프로그램이 정하는 경계. 처방에는 이 칸이 없다."""

    memory_gb: float
    cpus: float
    install_timeout_s: int
    step_timeout_s: int


def default_limits() -> Limits:
    """기본 상한. CPU는 Docker 호스트의 절반(최소 1)이다."""
    ncpu = int(docker_cli.run("info", "--format", "{{.NCPU}}").stdout.strip() or "1")
    return Limits(
        memory_gb=MEMORY_GB,
        cpus=max(1, ncpu // 2),
        install_timeout_s=INSTALL_TIMEOUT_S,
        step_timeout_s=STEP_TIMEOUT_S,
    )


@dataclass(frozen=True)
class Layer:
    name: LayerName
    key: str
    image: str  # pr-proof-env:<키>
    reused: bool
    seconds: float
    egress: tuple[Event, ...] = ()  # 구울 때 프록시가 받은 요청. 다시 쓴 층은 비었다


@dataclass(frozen=True)
class Layers:
    """구운 층. 마지막 층에서 작업 컨테이너를 띄운다."""

    digest: str  # 시작 이미지 `이름@sha256:…`
    layers: tuple[Layer, ...]

    @property
    def top(self) -> str:
        return self.layers[-1].image


def ensure_layers(
    recipe: EnvRecipe,
    source: Path,
    limits: Limits,
    *,
    names: docker_cli.Names = docker_cli.DEFAULT_NAMES,
) -> Layers | BakeFailed:
    """없는 층만 굽는다. source는 deps.inputs를 읽을 저장소 폴더다.

    docker나 네트워크가 실패하면 SandboxError를 올린다.
    """
    digest = resolve_image(recipe.system.image)
    if isinstance(digest, BakeFailed):
        return digest
    user = docker_cli.user()
    system = _ensure(
        "system",
        system_key(recipe, digest, user),
        lambda tag: _bake_system(recipe, digest, tag, user, limits, names),
    )
    if isinstance(system, BakeFailed):
        return system
    if recipe.deps is None:
        return Layers(digest, (system,))
    inputs = read_inputs(source, recipe.deps.inputs)
    if isinstance(inputs, BakeFailed):
        return inputs
    deps = _ensure(
        "deps",
        deps_key(recipe, system.key, user, inputs),
        lambda tag: _bake_deps(recipe, system.image, inputs, tag, user, limits, names),
    )
    return deps if isinstance(deps, BakeFailed) else Layers(digest, (system, deps))


def resolve_image(image: str) -> str | BakeFailed:
    """시작 이미지를 받아 `이름@sha256:…`으로 푼다. 없는 이미지는 처방의 잘못이다.

    네트워크가 닿지 않으면 받아 둔 것을 쓰고, 그것도 없으면 SandboxError다.
    """
    pulled = docker_cli.run("pull", "-q", image, timeout=_PULL_TIMEOUT_S, check=False)
    if pulled.returncode != 0:
        why = pulled.stderr.strip()
        local = _local_digest(image)
        network = install_pure.bake_failure_kind(why) == "network"
        if local is not None and network:
            log.warning("시작 이미지를 새로 받지 못해 있는 것을 씀: %s", why[-300:])
            return local
        if network or "Cannot connect to the Docker daemon" in why:
            raise SandboxError(f"docker pull 실패 ({pulled.returncode}): {why[-500:]}")
        return BakeFailed("system", f"system.image ({image})", pulled.returncode, why[-2000:])
    digest = _local_digest(image)
    if digest is None:
        raise SandboxError(f"받은 이미지의 digest를 읽지 못함: {image}")
    return digest


def _local_digest(image: str) -> str | None:
    proc = docker_cli.run(
        "image", "inspect", image, "--format", "{{json .RepoDigests}}", check=False
    )
    if proc.returncode != 0:
        return None
    try:
        digests = json.loads(proc.stdout)
    except ValueError:
        return None
    return install_pure.pick_digest(image, digests or [])


def read_inputs(source: Path, paths: tuple[str, ...]) -> dict[str, bytes] | BakeFailed:
    """deps.inputs를 저장소 폴더에서 읽는다. 링크를 거치거나 밖을 가리키면 처방의 잘못이다."""
    root = source.resolve()
    inputs: dict[str, bytes] = {}
    for rel in paths:
        path = source / rel
        data = None
        if path.resolve() == root / rel:
            data = read_regular_file(path, max_bytes=MAX_INPUT_BYTES)
        if data is None:
            why = (
                f"{rel}: 저장소 안의 일반 파일이 아님 (없음, 링크, 폴더, "
                f"{MAX_INPUT_BYTES // (1024 * 1024)}MB 초과 중 하나)"
            )
            return BakeFailed("deps", "deps.inputs", None, why)
        inputs[rel] = data
    return inputs


def _ensure(
    name: LayerName, key: str, bake: Callable[[str], BakeFailed | tuple[Event, ...]]
) -> Layer | BakeFailed:
    tag = f"{ENV_IMAGE}:{key}"
    if _image_exists(tag):
        return Layer(name, key, tag, reused=True, seconds=0.0)
    started = time.monotonic()
    baked = bake(tag)
    if isinstance(baked, BakeFailed):
        return baked
    if not _image_exists(tag):
        raise SandboxError(f"커밋한 층이 없음: {tag}")
    return Layer(name, key, tag, reused=False, seconds=time.monotonic() - started, egress=baked)


def _image_exists(tag: str) -> bool:
    return docker_cli.run("image", "inspect", tag, check=False).returncode == 0


def _bake_system(
    recipe: EnvRecipe, digest: str, tag: str, user: str, limits: Limits, names: docker_cli.Names
) -> BakeFailed | tuple[Event, ...]:
    """root로 처방의 명령과 받기를 돌리고 /opt/env를 호스트 UID에게 준다. 호스트 경로는 없다."""
    caps = [arg for cap in SYSTEM_CAPS for arg in ("--cap-add", cap)]
    args = (*docker_cli.resources(limits.memory_gb, limits.cpus),
            "--cap-drop", "ALL", *caps, *docker_cli.NO_NEW_PRIVILEGES)  # fmt: skip
    prepare = f"mkdir -p {SRC_DIR} {OVERLAY_DIR} && chown -R {user} {ENV_ROOT}"
    with _bake_box(names, digest, "system", args, limits) as box:
        return box.run_all(
            tag,
            box.probe,
            *(partial(box.shell, f"system.commands[{i}]", command)
              for i, command in enumerate(recipe.system.commands)),
            *(partial(box.fetch, f"system.fetches[{i}]", f.url, f.rev, f"{ENV_ROOT}/{f.dest}")
              for i, f in enumerate(recipe.system.fetches)),
            partial(box.shell, f"{ENV_ROOT} 준비", prepare),
        )  # fmt: skip


def _bake_deps(
    recipe: EnvRecipe,
    parent: str,
    inputs: dict[str, bytes],
    tag: str,
    user: str,
    limits: Limits,
    names: docker_cli.Names,
) -> BakeFailed | tuple[Event, ...]:
    """호스트 UID로 입력 파일과 덮어쓰기 파일을 복사해 넣고 처방의 명령을 돌린다."""
    assert recipe.deps is not None
    args = (*docker_cli.limits(limits.memory_gb, limits.cpus), "--user", user, "-w", SRC_DIR)
    files = {f"src/{path}": data for path, data in inputs.items()}
    files |= {f"overlay/{o.path}": o.content.encode() for o in recipe.overlays}
    with _bake_box(names, parent, "deps", args, limits) as box:
        box.copy_in(files)
        return box.run_all(
            tag,
            *(partial(box.shell, f"deps.commands[{i}]", command)
              for i, command in enumerate(recipe.deps.commands)),
        )  # fmt: skip


class _BakeBox:
    """굽는 컨테이너 하나. 명령은 층 하나의 시간 상한 안에서 차례로 넣는다."""

    def __init__(self, name: str, layer: LayerName, limits: Limits, net: Egress) -> None:
        self.name = name
        self.layer: LayerName = layer
        self.limits = limits
        self.net = net
        self.deadline = time.monotonic() + limits.install_timeout_s

    def run_all(
        self, tag: str, *steps: Callable[[], BakeFailed | None]
    ) -> BakeFailed | tuple[Event, ...]:
        """차례로 돌려 처음 실패에서 멈춘다. 모두 지나면 tag로 커밋하고 프록시의 기록을 준다."""
        for step in steps:
            failed = step()
            if failed is not None:
                return failed
        docker_cli.run(
            "commit", "--change", f"LABEL {ENV_LABEL}",
            self.name, tag, timeout=_COMMIT_TIMEOUT_S,
        )  # fmt: skip
        return self.net.events()

    def probe(self) -> BakeFailed | None:
        """이미지에 sh와 timeout이 있는지. 없으면 처방이 고른 이미지의 잘못이다."""
        result = run_in_sandbox(self.name, ["sh", "-c", f"mkdir -p {ENV_ROOT}"],
                                timeout=_PROBE_TIMEOUT_S)  # fmt: skip
        if result.returncode == 0:
            return None
        if result.returncode in (126, 127):
            why = "이미지에 sh와 timeout(coreutils)이 있어야 함\n" + result.stderr[-1000:]
            return BakeFailed(self.layer, "system.image", result.returncode, why)
        raise SandboxError(f"굽는 컨테이너를 쓰지 못함 ({result.returncode}): {result.stderr}")

    def shell(self, where: str, command: str) -> BakeFailed | None:
        return self._exec(where, ["sh", "-c", command])

    def fetch(self, where: str, url: str, rev: str, dest: str) -> BakeFailed | None:
        """컨테이너 안에서 clone하고, HEAD가 rev인지 호스트가 확인한다."""
        failed = self._exec(where, ["sh", "-c", _FETCH_SCRIPT, "sh", url, dest, rev])
        if failed is not None:
            return failed
        head = run_in_sandbox(self.name, ["git", "-C", dest, "rev-parse", "HEAD"],
                              timeout=_PROBE_TIMEOUT_S)  # fmt: skip
        if head.returncode != 0 or head.stdout.strip() != rev:
            why = f"받은 커밋이 rev와 다름: {head.stdout.strip()!r} != {rev!r} {head.stderr[-500:]}"
            return BakeFailed(self.layer, where, head.returncode, why)
        return None

    def copy_in(self, files: dict[str, bytes]) -> None:
        """파일을 호스트 UID 소유로 /opt/env 아래에 넣는다. 마운트하지 않고 복사한다."""
        staging = Path(tempfile.mkdtemp(prefix="pr-proof-env-"))
        try:
            for sub in ("src", "overlay"):
                (staging / sub).mkdir()
            for rel, data in files.items():
                path = staging / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            docker_cli.run("cp", "-a", f"{staging}/.", f"{self.name}:{ENV_ROOT}/", timeout=300)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _exec(self, where: str, argv: list[str]) -> BakeFailed | None:
        left = int(self.deadline - time.monotonic())
        if left <= 0:
            return self._timed_out(where, "")
        seen = len(self.net.events())
        result = run_in_sandbox(self.name, argv, env=self.net.env(), timeout=left)
        tail = install_pure.output_tail(result.stdout, result.stderr, MAX_OUTPUT_CHARS)
        if result.timed_out:
            return self._timed_out(where, tail + self._egress_note(where, seen, tail))
        if result.returncode == 0:
            return None
        if install_pure.is_docker_failure(result.returncode, result.stderr):
            raise SandboxError(f"docker exec 실패 ({result.returncode}): {result.stderr[-500:]}")
        tail += self._egress_note(where, seen, tail)
        state = docker_cli.container_state(self.name)
        if state is not None and state.oom_killed:
            tail += f"\n(메모리 한도 {self.limits.memory_gb:g}GB를 넘어 종료됨)"
        elif state is None or not state.running:
            raise SandboxError(f"굽는 컨테이너 {self.name}가 실행 중이 아님 ({result.returncode})")
        return BakeFailed(self.layer, where, result.returncode, tail)

    def _egress_note(self, where: str, seen: int, tail: str) -> str:
        """실패한 단계에서 프록시가 받은 요청을 본다. 네트워크 자체가 닿지 않았으면 환경 오류다."""
        events = self.net.events()[seen:]
        down = install_pure.network_down(events)
        if down is not None:
            raise SandboxError(
                f"설치 중 네트워크 실패 ({where}): {down.verdict} {down.target()} {down.detail}"
            )
        return install_pure.egress_note(events, tail)

    def _timed_out(self, where: str, tail: str) -> BakeFailed:
        why = f"{tail}\n(층 하나의 시간 상한 {self.limits.install_timeout_s}초를 넘음)".strip()
        return BakeFailed(self.layer, where, None, why, timed_out=True)


@contextmanager
def _bake_box(
    names: docker_cli.Names, image: str, layer: LayerName, args: tuple[str, ...], limits: Limits
) -> Iterator[_BakeBox]:
    """굽는 컨테이너를 띄운다(내부 네트워크와 프록시, 호스트 경로 없음). 블록을 나가면 지운다.

    시작 이미지의 ENTRYPOINT를 비워 커밋한 층에도 남지 않게 한다. 작업 컨테이너가 `sleep infinity`로
    뜬다. 프록시 변수는 커밋한 층에 남지 않게 명령마다 넘긴다.
    """
    lifetime = limits.install_timeout_s + _COMMIT_TIMEOUT_S + _EGRESS_SLACK_S
    with egress.opened(names, lifetime) as net:
        name = f"{names.container_prefix}-bake-{uuid.uuid4().hex[:12]}"
        docker_cli.run(
            "run", "-d", "--name", name, "--network", net.network, *args, "-e", "HOME=/tmp",
            "--entrypoint", "", image, "sleep", "infinity",
        )  # fmt: skip
        try:
            yield _BakeBox(name, layer, limits, net)
        finally:
            docker_cli.remove_container(name)
