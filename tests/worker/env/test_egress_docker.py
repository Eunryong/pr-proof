"""굽는 컨테이너의 네트워크 통합 테스트 (DESIGN 8.1, 위협 T41). 실제 Docker로 굽는다.

굽는 컨테이너는 내부 네트워크에만 붙고 프록시로 공용 주소에만 닿는다. 시스템 층이 apt로 curl을
설치하고(평문 HTTP가 프록시를 거친다), 의존성 층에서 https 받기와 사설 주소로 가는 연결을 본다.
"""

import subprocess
import time
import uuid
from pathlib import Path

import pytest

from app.worker.env import BakeFailed, EnvRecipe, Layers, Limits, ensure_layers
from app.worker.env import validate as validate_recipe
from app.worker.env.install_pure import NO_PROXY_HINT
from app.worker.sandbox import egress
from app.worker.sandbox.egress_pure import UNTIL_LABEL
from tests.sandbox_names import TEST_NAMES

pytestmark = pytest.mark.docker

LIMITS = Limits(memory_gb=1, cpus=1, install_timeout_s=300, step_timeout_s=60)
SYSTEM = {
    "image": "debian:bookworm-slim",
    "commands": [
        "apt-get update && apt-get install -y --no-install-recommends ca-certificates curl"
    ],
}


def _docker(*args: str) -> str:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture(scope="module")
def private_targets() -> dict[str, str]:
    """URL과 프록시가 볼 `호스트:포트`. 호스트(Redis)는 이름과 주소, Docker bridge 게이트웨이,
    클라우드 메타데이터."""
    gateway = _docker("network", "inspect", "bridge", "--format",
                      "{{(index .IPAM.Config 0).Gateway}}").strip()  # fmt: skip
    host_ip = _docker("run", "--rm", "debian:bookworm-slim", "getent", "hosts",
                      "host.docker.internal").split()[0]  # fmt: skip
    return {
        "http://host.docker.internal:6379/": "host.docker.internal:6379",
        f"http://{host_ip}:6379/": f"{host_ip}:6379",
        f"http://{gateway}:6379/": f"{gateway}:6379",
        "http://169.254.169.254/latest/meta-data/": "169.254.169.254:80",
    }


def _recipe(*deps: str) -> EnvRecipe:
    recipe = validate_recipe({"system": SYSTEM, "deps": {"commands": list(deps)}})
    assert isinstance(recipe, EnvRecipe), recipe
    return recipe


def _leftovers() -> tuple[str, str]:
    prefix = TEST_NAMES.container_prefix
    containers = _docker("ps", "-a", "--format", "{{.Names}}", "--filter", f"name={prefix}-")
    networks = _docker("network", "ls", "--format", "{{.Name}}", "--filter",
                       f"label={UNTIL_LABEL}", "--filter", f"name={prefix}-")  # fmt: skip
    return containers.strip(), networks.strip()


def test_bake_reaches_public_https_but_no_private_address(
    tmp_path: Path, private_targets: dict[str, str]
) -> None:
    must_fail = " && ".join(
        f"! curl -fsS --max-time 10 {flag} {url}"
        for url in private_targets
        for flag in ("", "--noproxy '*'")
    )
    fresh = f"echo {uuid.uuid4().hex}"  # 의존성 층을 다시 쓰지 않고 굽는다
    recipe = _recipe(f"curl -fsS -o /dev/null https://github.com/ && {fresh}", must_fail)
    layers = ensure_layers(recipe, tmp_path, LIMITS, names=TEST_NAMES)
    assert isinstance(layers, Layers), layers
    system, deps = layers.layers
    if not system.reused:
        assert "deb.debian.org:80" in {e.target() for e in system.egress}
    allowed = {e.target() for e in deps.egress if e.verdict == "allowed"}
    denied = {e.target() for e in deps.egress if e.verdict == "denied"}
    assert allowed == {"github.com:443"}
    assert denied == set(private_targets.values())
    assert _leftovers() == ("", "")


def test_denied_address_fails_the_step_as_a_recipe_error_and_cleans_up(tmp_path: Path) -> None:
    recipe = _recipe(f"echo {tmp_path.name}; curl -fsS --max-time 10 http://169.254.169.254/")
    failed = ensure_layers(recipe, tmp_path, LIMITS, names=TEST_NAMES)
    assert isinstance(failed, BakeFailed), failed
    assert failed.step == "deps.commands[0]"
    assert "공용 주소가 아니라 거절: GET 169.254.169.254:80" in failed.output_tail
    assert _leftovers() == ("", "")


def test_name_that_does_not_resolve_is_a_recipe_error_with_the_host(tmp_path: Path) -> None:
    host = f"no-such-host-{tmp_path.name.lower().replace('_', '-')}.invalid"
    failed = ensure_layers(_recipe(f"curl -fsS https://{host}/"), tmp_path, LIMITS,
                           names=TEST_NAMES)  # fmt: skip
    assert isinstance(failed, BakeFailed), failed
    assert f"이름이 없음: CONNECT {host}:443" in failed.output_tail


def test_tool_that_skips_the_proxy_is_told_why(tmp_path: Path) -> None:
    command = f"echo {tmp_path.name}; curl -fsS --max-time 10 --noproxy '*' https://github.com/"
    failed = ensure_layers(_recipe(command), tmp_path, LIMITS, names=TEST_NAMES)
    assert isinstance(failed, BakeFailed), failed
    assert "Could not resolve host" in failed.output_tail
    assert failed.output_tail.endswith(NO_PROXY_HINT)


def test_interrupted_session_removes_proxy_and_network() -> None:
    with pytest.raises(KeyboardInterrupt), egress.opened(TEST_NAMES, 600) as net:
        assert net.proxy in _docker("ps", "--format", "{{.Names}}")
        raise KeyboardInterrupt
    assert _leftovers() == ("", "")


def test_sweep_removes_what_a_dead_worker_left_after_its_deadline() -> None:
    prefix = f"{TEST_NAMES.container_prefix}-sweep"
    now = int(time.time())
    for name, until in ((f"{prefix}-old", now - 1), (f"{prefix}-live", now + 600)):
        _docker("network", "create", "--internal", "--label", f"{UNTIL_LABEL}={until}", name)
        _docker("run", "-d", "--name", f"{name}-box", "--network", name,
                "debian:bookworm-slim", "sleep", "infinity")  # fmt: skip
    try:
        egress.sweep(now)
        networks = _docker("network", "ls", "--format", "{{.Name}}", "--filter", f"name={prefix}")
        containers = _docker("ps", "-a", "--format", "{{.Names}}", "--filter", f"name={prefix}")
        assert networks.split() == [f"{prefix}-live"]
        assert containers.split() == [f"{prefix}-live-box"]
    finally:
        egress.close(f"{prefix}-live")
        egress.close(f"{prefix}-old")
    assert _leftovers() == ("", "")
