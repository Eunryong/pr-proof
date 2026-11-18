"""대상 리포의 .venv 준비와 캐시 (docs/DESIGN.md F5).

.venv에는 의존성만 설치하고 대상 리포 자체는 설치하지 않는다. 설치는 네트워크가 열린
별도 컨테이너에서 하고, 테스트 컨테이너에는 읽기 전용으로 마운트한다.

설치 컨테이너 (8.1): wheel만 받는다(`--only-binary :all:`). 소스 배포본은 설치 중에 빌드 코드를
실행하는데 그 코드는 PR이 의존성 파일로 고를 수 있다. 호스트의 것은 마운트하지 않는다. 의존성
파일은 표준 입력으로 복사해 넣고, 붙이는 호스트 경로는 그 설치만의 빈 출력 폴더 하나다.
"""

import hashlib
import json
import re
import shlex
import shutil
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
DEPS_IN_CONTAINER: Final = "/tmp/deps"
# 설치 방법의 버전. 설치 명령을 바꾸면 올린다 (캐시 키에 들어간다)
INSTALL_VERSION: Final = "binary-only-1"
# 표준 입력의 {파일 이름: 내용}을 DEPS_IN_CONTAINER에 쓰고, wheel만 설치한다. pip의 자세한
# 기록(-vv)은 실패했을 때만 줄여서 내보낸다: 건너뛴 링크 줄을 뺀 끝부분과, 소스 배포본만 있어
# 건너뛴 패키지 이름
_INSTALL_SCRIPT: Final = """\
python -c 'import json, os, sys
files = json.load(sys.stdin)
os.makedirs(sys.argv[1])
for name, text in files.items():
    with open(os.path.join(sys.argv[1], os.path.basename(name)), "w", encoding="utf-8") as f:
        f.write(text)' {deps} || exit 3
python -m venv {venv} || exit 4
{venv}/bin/pip install -vv --no-cache-dir --only-binary :all: {args} > /tmp/pip.log 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then
  grep -v '^ *Skipping link' /tmp/pip.log | tail -c 4000
  grep -o 'No sources permitted for [^:]*' /tmp/pip.log | sort -u
fi
exit "$rc"
"""

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
_NO_MATCH: Final = re.compile(r"No matching distribution found for ([A-Za-z0-9][A-Za-z0-9._-]*)")
_SOURCES_ONLY: Final = re.compile(r"No sources permitted for ([A-Za-z0-9][A-Za-z0-9._-]*)")
# 의존성 파일에서 받는 옵션 줄: 같이 넘긴 다른 의존성 파일을 읽는 것뿐이다. `--no-binary`,
# 색인 주소, `-e` 같은 옵션은 wheel만 받는다는 제한을 풀거나 다른 곳에서 받게 하므로 설치하지 않는다
_INCLUDE: Final = re.compile(r"^(?:-r|-c|--requirement|--constraint)[\s=]+([^\s/\\]+)$")
# 이름과 버전 조건이 아니라 주소·경로로 고른 의존성 (소스를 받아 빌드한다)
_DIRECT: Final = re.compile(r"://|\s@\s|^[./~]|^file:", re.IGNORECASE)


class DependencyInstallError(Exception):
    """pip이 의존성을 설치하지 못했다. 처리는 models.install.install_failure_outcome()이 정한다."""

    def __init__(
        self, kind: InstallFailureKind, cache_key: str, detail: str, packages: tuple[str, ...] = ()
    ) -> None:
        super().__init__(f"의존성 설치 실패 ({kind}): {detail[-500:]}")
        self.kind: InstallFailureKind = kind
        self.cache_key = cache_key
        self.detail = detail
        self.packages = packages  # source_only: 소스 배포본만 있는 패키지


def classify_pip_failure(output: str) -> InstallFailureKind:
    """pip 출력으로 실패 원인을 나눈다."""
    if any(marker in output for marker in NETWORK_MARKERS):
        return "network"
    if source_only_packages(output):
        return "source_only"
    if any(marker in output for marker in RESOLUTION_MARKERS):
        return "resolution"
    return "unknown"


def disallowed_lines(files: dict[str, str]) -> list[str]:
    """설치하지 않는 의존성 줄 (8.1 바이너리만 설치). 비면 설치해도 된다."""
    bad = []
    for name, text in files.items():
        for raw in text.splitlines():
            line = raw.split(" #", 1)[0].strip()
            if not line or line.startswith("#"):
                continue
            include = _INCLUDE.match(line)
            if include is not None:
                if include.group(1) not in files:
                    bad.append(f"{name}: {line}")
            elif line.startswith("-") or _DIRECT.search(line.split(";", 1)[0]):
                bad.append(f"{name}: {line}")
    return bad


def source_only_packages(output: str) -> tuple[str, ...]:
    """찾지 못한 패키지 중 소스 배포본은 있어 건너뛴 것 (`--only-binary :all:`의 -vv 기록).

    pip은 wheel이 없는 패키지와 없는 패키지를 같은 오류("No matching distribution")로 알린다.
    건너뛴 링크 기록에 그 이름이 있으면 소스 배포본만 있는 패키지다.
    """
    missing = {_normalize(m) for m in _NO_MATCH.findall(output)}
    skipped = {_normalize(m) for m in _SOURCES_ONLY.findall(output)}
    return tuple(sorted(missing & skipped))


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
    """의존성 파일 내용 + 베이스 이미지 id + Python 버전 + 검사 도구 버전 + 설치 방법 버전."""
    h = hashlib.sha256()
    for part in (image.id, image.python_version, *TOOL_REQUIREMENTS, INSTALL_VERSION):
        h.update(part.encode() + b"\0")
    for path in dependency_files(worktree):
        h.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return h.hexdigest()[:24]


def deps_input(worktree: Path) -> tuple[dict[str, str], list[str]]:
    """설치 컨테이너에 표준 입력으로 넘길 의존성 파일 {이름: 내용}과 pip 인자."""
    files: dict[str, str] = {}
    args: list[str] = []
    for path in dependency_files(worktree):
        if path.name == "pyproject.toml":
            reqs = pyproject_requirements(path.read_text())
            name, text = "pyproject-deps.txt", "\n".join(reqs) + "\n"
        else:
            name, text = path.name, path.read_text(encoding="utf-8", errors="replace")
        files[name] = text
        args += ["-r", f"{DEPS_IN_CONTAINER}/{name}"]
    return files, [*args, *TOOL_REQUIREMENTS]


def install_script(pip_args: list[str]) -> str:
    return _INSTALL_SCRIPT.format(
        deps=DEPS_IN_CONTAINER, venv=VENV_IN_CONTAINER, args=shlex.join(pip_args)
    )


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
        files, pip_args = deps_input(worktree)
        bad = disallowed_lines(files)
        if bad:
            detail = "wheel만 설치하므로 받지 않는 줄:\n" + "\n".join(bad[:20])
            raise DependencyInstallError("disallowed", key, detail[:MAX_DETAIL_CHARS])
        proc = docker_cli.run(
            "run", "--rm", "-i", *docker_cli.limits(memory_gb),
            "--user", docker_cli.user(), "-e", "HOME=/tmp",
            "-v", f"{build}:{VENV_IN_CONTAINER}",
            tag, "sh", "-c", install_script(pip_args),
            timeout=INSTALL_TIMEOUT, check=False, input=json.dumps(files),
        )  # fmt: skip
        if proc.returncode in docker_cli.DOCKER_FAILED:
            raise docker_cli.SandboxError(
                f"설치 컨테이너 실행 실패 ({proc.returncode}): {proc.stderr.strip()[-500:]}"
            )
        if proc.returncode != 0:
            output = proc.stdout + proc.stderr
            raise DependencyInstallError(
                classify_pip_failure(output), key, output[-MAX_DETAIL_CHARS:],
                packages=source_only_packages(output),
            )  # fmt: skip
        marker = build / ".complete"
        marker.unlink(missing_ok=True)  # 설치 컨테이너가 남긴 것(링크일 수 있음)을 따라가지 않는다
        marker.touch(exist_ok=False)
        try:
            build.rename(target)
        except OSError:  # 다른 작업이 같은 키를 먼저 만들었다
            if not (target / ".complete").is_file():
                raise
    finally:
        if build.exists():
            shutil.rmtree(build, ignore_errors=True)
    return target
