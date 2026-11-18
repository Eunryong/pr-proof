"""대상 리포의 .venv 준비와 캐시 (docs/DESIGN.md F5).

.venv에는 의존성만 설치하고 대상 리포 자체는 설치하지 않는다. 설치는 네트워크가 열린
별도 컨테이너에서 하고, 테스트 컨테이너에는 읽기 전용으로 마운트한다.
"""

import hashlib
import re
import shlex
import shutil
import tempfile
import tomllib
import uuid
from pathlib import Path
from typing import Final

from app.models.install import InstallFailureKind
from app.sandbox import docker_cli
from app.sandbox.docker_cli import Image

VENV_CACHE: Final = Path.home() / ".cache" / "pipeline" / "venvs"
# 리포 의존성과 함께 설치하는 검사 도구. pytest 자체는 리포가 고정한 버전을 따른다.
TOOL_REQUIREMENTS: Final = ("pytest-cov==6.0.0", "pytest-json-report==1.5.0")
INSTALL_TIMEOUT: Final = 900
# 컨테이너 안 .venv 경로. 스크립트의 인터프리터 경로가 여기에 묶이므로 설치·실행이 같아야 한다.
VENV_IN_CONTAINER: Final = "/work/.venv"

# pip 출력으로 설치 실패의 원인을 나눈다. 네트워크 실패에서도 "Could not find a version"이
# 함께 나오므로 네트워크 표지를 먼저 본다.
NETWORK_MARKERS: Final = (
    "NewConnectionError",
    "Failed to establish a new connection",
    "Temporary failure in name resolution",
    "Name or service not known",
    "ConnectTimeoutError",
    "ReadTimeoutError",
    "Connection reset by peer",
    "ProxyError",
    "SSLError",
)
RESOLUTION_MARKERS: Final = (
    "No matching distribution found",
    "ResolutionImpossible",
    "Could not find a version that satisfies the requirement",
    "conflicting dependencies",
    "Invalid requirement",
)
MAX_DETAIL_CHARS: Final = 2000


class DependencyInstallError(Exception):
    """pip이 의존성을 설치하지 못했다. 처리는 models.install.install_failure_outcome()이 정한다."""

    def __init__(self, kind: InstallFailureKind, cache_key: str, detail: str) -> None:
        super().__init__(f"의존성 설치 실패 ({kind}): {detail[-500:]}")
        self.kind: InstallFailureKind = kind
        self.cache_key = cache_key
        self.detail = detail


def classify_pip_failure(output: str) -> InstallFailureKind:
    """pip 출력으로 실패 원인을 나눈다."""
    if any(marker in output for marker in NETWORK_MARKERS):
        return "network"
    if any(marker in output for marker in RESOLUTION_MARKERS):
        return "resolution"
    return "unknown"


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def pyproject_requirements(text: str) -> list[str]:
    """pyproject.toml의 의존성과 모든 optional-dependencies. 프로젝트 자신은 뺀다."""
    project = tomllib.loads(text).get("project", {})
    reqs = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        reqs.extend(group)
    own = _normalize(project.get("name", ""))
    return [
        r for r in reqs if _normalize(re.split(r"[\s\[<>=!~;@]", r.strip(), maxsplit=1)[0]) != own
    ]


def dependency_files(worktree: Path) -> list[Path]:
    """의존성 파일: 루트의 requirements*.txt와 pyproject.toml. lock 파일은 아직 쓰지 않는다."""
    files = sorted(worktree.glob("requirements*.txt"))
    if (worktree / "pyproject.toml").is_file():
        files.append(worktree / "pyproject.toml")
    return files


def cache_key(worktree: Path, image: Image) -> str:
    """의존성 파일 내용 + 베이스 이미지 id + Python 버전 + 검사 도구 버전."""
    h = hashlib.sha256()
    for part in (image.id, image.python_version, *TOOL_REQUIREMENTS):
        h.update(part.encode() + b"\0")
    for path in dependency_files(worktree):
        h.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return h.hexdigest()[:24]


def write_deps(worktree: Path, dest: Path) -> list[str]:
    """설치 컨테이너에 넘길 의존성 파일을 dest에 만들고 pip 인자를 돌려준다."""
    args: list[str] = []
    for path in dependency_files(worktree):
        if path.name == "pyproject.toml":
            reqs = pyproject_requirements(path.read_text())
            (dest / "pyproject-deps.txt").write_text("\n".join(reqs) + "\n")
            args += ["-r", "/deps/pyproject-deps.txt"]
        else:
            shutil.copy2(path, dest / path.name)
            args += ["-r", f"/deps/{path.name}"]
    return [*args, *TOOL_REQUIREMENTS]


def ensure_venv(
    worktree: Path,
    image: Image,
    cache_dir: Path = VENV_CACHE,
    tag: str = docker_cli.IMAGE,
    *,
    memory_gb: float = docker_cli.MEMORY_LIMIT_GB,
) -> Path:
    """캐시 키에 맞는 .venv 경로를 돌려준다. 없으면 설치 컨테이너에서 만든다.

    임시 폴더에 설치하고 완료 표시를 남긴 뒤 캐시 경로로 옮겨, 실패한 설치가 캐시에 남지 않게 한다.
    docker 자체가 실패하면 SandboxError, pip이 실패하면 DependencyInstallError를 올린다.
    """
    key = cache_key(worktree, image)
    target = cache_dir / key
    if (target / ".complete").is_file():
        return target

    cache_dir.mkdir(parents=True, exist_ok=True)
    build = cache_dir / f".{key}.{uuid.uuid4().hex[:8]}.tmp"
    build.mkdir()
    try:
        with tempfile.TemporaryDirectory(prefix="pipeline-deps-") as deps:
            pip_args = write_deps(worktree, Path(deps))
            script = (
                f"python -m venv {VENV_IN_CONTAINER}"
                f" && {VENV_IN_CONTAINER}/bin/pip install --no-cache-dir " + shlex.join(pip_args)
            )
            proc = docker_cli.run(
                "run", "--rm", *docker_cli.limits(memory_gb),
                "--user", docker_cli.user(), "-e", "HOME=/tmp",
                "-v", f"{deps}:/deps:ro",
                "-v", f"{build}:{VENV_IN_CONTAINER}",
                tag, "sh", "-c", script,
                timeout=INSTALL_TIMEOUT, check=False,
            )  # fmt: skip
        if proc.returncode in docker_cli.DOCKER_FAILED:
            raise docker_cli.SandboxError(
                f"설치 컨테이너 실행 실패 ({proc.returncode}): {proc.stderr.strip()[-500:]}"
            )
        if proc.returncode != 0:
            output = (proc.stdout + proc.stderr)[-MAX_DETAIL_CHARS:]
            raise DependencyInstallError(classify_pip_failure(output), key, output)
        (build / ".complete").touch()
        try:
            build.rename(target)
        except OSError:  # 다른 작업이 같은 키를 먼저 만들었다
            if not (target / ".complete").is_file():
                raise
    finally:
        if build.exists():
            shutil.rmtree(build, ignore_errors=True)
    return target
