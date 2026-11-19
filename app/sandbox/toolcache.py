"""도구 캐시 (docs/DESIGN.md 2.3, 4.4 도구 준비, 8.1 설치 컨테이너).

도구는 미리 깔지 않는다. 규칙이 그 도구를 쓰기로 정해졌을 때 `ensure`가 받아 (도구, 버전, 플랫폼)
폴더에 두고, 실행 컨테이너는 그 폴더를 읽기 전용으로 마운트한다. 버전은 저장소가 고정한 것을 먼저
쓰고, 없으면 도구의 기본 버전이다. 해시를 고정해 둔 버전만 받는다.

받는 일은 네트워크가 열린 설치 컨테이너만 한다. 그 컨테이너에는 빈 출력 폴더 하나만 붙이고,
받을 URL만 인자로 넘긴다. 받은 파일은 호스트가 고정해 둔 SHA256과 맞춰 본 뒤에만 캐시에 넣는다.
임시 폴더에서 만들고 완료 표시를 쓴 다음 제자리로 옮기므로, 중단되거나 동시에 받아도 캐시에는
완성된 폴더만 남는다.
"""

import hashlib
import io
import shutil
import tarfile
import time
import uuid
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.sandbox import docker_cli
from app.sandbox.safe_read import read_regular_file

TOOL_CACHE: Final = Path.home() / ".cache" / "pipeline" / "tools"
COMPLETE: Final = ".complete"
CHECKSUM_FILE: Final = "SHA256"
FETCH_TIMEOUT: Final = 300
MAX_ARCHIVE_BYTES: Final = 64 * 1024 * 1024
MAX_BINARY_BYTES: Final = 256 * 1024 * 1024
# 이보다 오래된 임시 폴더는 중단된 설치가 남긴 것이다. 진행 중인 설치는 FETCH_TIMEOUT 안에 끝난다
STALE_TMP_SECONDS: Final = FETCH_TIMEOUT * 4
# 실행 이미지(docker_cli.TOOL_IMAGE)가 glibc 기반이라 gnu 빌드를 받는다
LIBC: Final = "gnu"
_ARCH: Final[Mapping[str, str]] = {
    "arm64": "arm64",
    "aarch64": "arm64",
    "amd64": "amd64",
    "x86_64": "amd64",
}
# 설치 컨테이너 안에서 도는 받기 스크립트. https만, 크기 상한까지만 받는다
_FETCH_SCRIPT: Final = """\
import sys, urllib.request
url, dest, limit = sys.argv[1], sys.argv[2], int(sys.argv[3])
if not url.startswith("https://"):
    sys.exit("https URL만 받는다")
with urllib.request.urlopen(url, timeout=60) as r, open(dest, "wb") as f:
    total = 0
    while chunk := r.read(1 << 20):
        total += len(chunk)
        if total > limit:
            sys.exit("크기 상한을 넘음")
        f.write(chunk)
"""


@dataclass(frozen=True)
class Asset:
    url: str
    sha256: str  # 받은 압축 파일의 해시. 처음 한 번 받아 계산하고 GitHub의 digest와 맞춰 봤다
    member: str  # 압축 파일(zip 또는 tar.gz) 안의 실행 파일 경로


@dataclass(frozen=True)
class Tool:
    name: str
    binary: str  # 캐시 폴더에 두는 실행 파일 이름
    default: str  # 저장소가 버전을 고정하지 않았을 때 쓰는 버전
    releases: Mapping[str, Mapping[str, Asset]]  # 버전 → 플랫폼 → 받을 파일


def _ast_grep(
    versions: Mapping[str, Mapping[str, tuple[str, str]]],
) -> Mapping[str, Mapping[str, Asset]]:
    base = "https://github.com/ast-grep/ast-grep/releases/download"
    return {
        version: {
            platform: Asset(f"{base}/{version}/app-{triple}.zip", sha256, "ast-grep")
            for platform, (triple, sha256) in sums.items()
        }
        for version, sums in versions.items()
    }


def _ruff(
    versions: Mapping[str, Mapping[str, tuple[str, str]]],
) -> Mapping[str, Mapping[str, Asset]]:
    base = "https://github.com/astral-sh/ruff/releases/download"
    return {
        version: {
            platform: Asset(f"{base}/{version}/ruff-{triple}.tar.gz", sha256, f"ruff-{triple}/ruff")
            for platform, (triple, sha256) in sums.items()
        }
        for version, sums in versions.items()
    }


_ARM: Final = "aarch64-unknown-linux-gnu"
_X86: Final = "x86_64-unknown-linux-gnu"

# 설치할 수 있는 도구와 버전은 이 목록뿐이다 (8.1). 모델이나 PR이 다른 것을 받게 할 수 없다.
# ruff의 기본 버전은 v1 샌드박스 이미지에 들어 있던 버전이다. 저장소가 목록 밖의 버전을 고정하면
# 그 도구를 쓰는 규칙은 "검사하지 못함 (도구를 준비하지 못함)"이다 (4.4)
TOOLS: Final[Mapping[str, Tool]] = {
    "ast-grep": Tool(
        name="ast-grep",
        binary="ast-grep",
        default="0.51.3",
        releases=_ast_grep({
            "0.51.3": {
                "linux-arm64-gnu": (
                    _ARM, "5b44fd68e78744934b359b07badce347d4197ed50b341d50f1f99d056ecedb08"
                ),
                "linux-amd64-gnu": (
                    _X86, "22803eb0c5bf83164fb7fd76d4721d355ecf0c8466ef47342c1ec7d80897ca47"
                ),
            },
        }),
    ),
    "ruff": Tool(
        name="ruff",
        binary="ruff",
        default="0.17.5",
        releases=_ruff({
            "0.17.6": {
                "linux-arm64-gnu": (
                    _ARM, "d4eea36ee6228f6c6bbcf0aea4ebf66fe0e595eedc6cffe628d7b8ba3623390f"
                ),
                "linux-amd64-gnu": (
                    _X86, "94b33791f48cd54ab35ad673a443a5c001d3e6d87effeaba9899070b700d45f3"
                ),
            },
            "0.17.5": {
                "linux-arm64-gnu": (
                    _ARM, "4d959d4322a8cd3effe11e9fc6743da1dbb41cd0b2476e433b6e6bfd9a69b626"
                ),
                "linux-amd64-gnu": (
                    _X86, "7d261fc981a784668e19e9fa6ad437bfd95b67f2dc263fc1eab87d54d72fcc90"
                ),
            },
            "0.17.4": {
                "linux-arm64-gnu": (
                    _ARM, "b939676db4532f748dc38c41361a29260f18b3e24a787d46d9278e449c25c2cf"
                ),
                "linux-amd64-gnu": (
                    _X86, "3ab2b34aeacdbc1ea5c529cdbda3e05792633d587228123fd09f3b5a0facb27a"
                ),
            },
            "0.16.10": {
                "linux-arm64-gnu": (
                    _ARM, "dc0d74de837ef0a7bcc62ce98c48a622b075d057161f13b958be2934becd55a6"
                ),
                "linux-amd64-gnu": (
                    _X86, "9567ff1201e2fb3da31ff04c35587d768c66d6cb42dfa84de474e2bfe360b608"
                ),
            },
            "0.15.22": {
                "linux-arm64-gnu": (
                    _ARM, "54ec426d839d7cea1096e9ea1c5486fd2f3df62ee6cfd71dc090b18f99bebd90"
                ),
                "linux-amd64-gnu": (
                    _X86, "d535a4be6504146e757eff67b992f11a293a7a108be22e2a5898b32c32565996"
                ),
            },
        }),
    ),
}  # fmt: skip


class ToolUnavailable(Exception):
    """이 플랫폼에 그 도구가 없거나 받은 파일이 고정한 해시와 다르다. 그 도구를 쓰는 규칙만
    "검사하지 못함 (도구를 준비하지 못함)"으로 둔다. 캐시에 남기지 않으므로 다음 검사가 다시 받는다.
    """


# (받을 파일, 압축 파일을 쓸 호스트 경로) → None. 받지 못하면 SandboxError
Download = Callable[[Asset, Path], None]


def platform_key(os_name: str, arch: str) -> str:
    """docker 데몬의 (os, arch)를 캐시 키의 플랫폼으로. 모르는 arch면 ToolUnavailable."""
    known = _ARCH.get(arch.lower())
    if known is None:
        raise ToolUnavailable(f"지원하지 않는 아키텍처: {arch}")
    return f"{os_name.lower()}-{known}-{LIBC}"


def docker_platform() -> str:
    """컨테이너가 도는 플랫폼. 호스트가 아니라 docker 데몬의 것이다 (macOS에서도 linux)."""
    out = docker_cli.run("version", "--format", "{{.Server.Os}}/{{.Server.Arch}}", timeout=30)
    os_name, _, arch = out.stdout.strip().partition("/")
    return platform_key(os_name, arch)


def tool_dir(cache_dir: Path, tool: Tool, version: str, platform: str) -> Path:
    return cache_dir / tool.name / version / platform


def is_known(name: str, version: str, tools: Mapping[str, Tool] = TOOLS) -> bool:
    """해시를 고정해 둔 버전인지. 받지 않고 본다."""
    tool = tools.get(name)
    return tool is not None and version in tool.releases


def docker_download(asset: Asset, archive: Path) -> None:
    """설치 컨테이너에서 받는다. 붙이는 호스트 경로는 archive가 든 빈 폴더 하나뿐이다."""
    out = archive.parent
    proc = docker_cli.run(
        "run", "--rm", "--read-only", *docker_cli.limits(),
        "--user", docker_cli.user(), "-e", "HOME=/tmp",
        "-v", f"{out.resolve()}:/out",
        docker_cli.TOOL_IMAGE,
        "python", "-I", "-c", _FETCH_SCRIPT, asset.url, f"/out/{archive.name}",
        str(MAX_ARCHIVE_BYTES),
        timeout=FETCH_TIMEOUT, check=False,
    )  # fmt: skip
    if proc.returncode != 0:
        raise docker_cli.SandboxError(
            f"도구를 받지 못함 ({proc.returncode}): {proc.stderr.strip()[-500:]}"
        )


def ensure(
    name: str,
    version: str | None = None,
    *,
    cache_dir: Path = TOOL_CACHE,
    platform: str | None = None,
    download: Download | None = None,
    tools: Mapping[str, Tool] = TOOLS,
) -> Path:
    """그 도구의 캐시 폴더(실행 파일 `Tool.binary`가 든 곳). 없으면 받아서 만든다.

    version이 없으면 도구의 기본 버전이다. 목록에 없는 도구·버전, 이 플랫폼의 배포본이 없음, 해시
    불일치는 ToolUnavailable. 받기 자체의 실패(네트워크, docker)는 SandboxError로 환경 오류다.
    """
    tool = tools.get(name)
    if tool is None:
        raise ToolUnavailable(f"도구 목록에 없음: {name}")
    version = version or tool.default
    release = tool.releases.get(version)
    if release is None:
        raise ToolUnavailable(f"{name} {version}은 파이프라인이 해시를 고정한 버전이 아님")
    platform = platform or docker_platform()
    asset = release.get(platform)
    if asset is None:
        raise ToolUnavailable(f"{name} {version}의 {platform} 배포본이 없음")
    target = tool_dir(cache_dir, tool, version, platform)
    if (target / COMPLETE).is_file():
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    _remove_stale(target.parent)
    build = target.parent / f".{platform}.{uuid.uuid4().hex[:8]}.tmp"
    download_dir = build / "download"
    download_dir.mkdir(parents=True)
    try:
        archive = download_dir / "archive"
        (download or docker_download)(asset, archive)
        data = read_regular_file(archive, max_bytes=MAX_ARCHIVE_BYTES)
        if data is None:
            raise docker_cli.SandboxError(f"받은 파일을 읽지 못함: {asset.url}")
        digest = hashlib.sha256(data).hexdigest()
        if digest != asset.sha256:
            raise ToolUnavailable(
                f"{name} {version} 해시 불일치: 받은 {digest}, 고정한 {asset.sha256}"
            )
        _extract(data, asset, build / tool.binary)
        shutil.rmtree(download_dir)
        (build / CHECKSUM_FILE).write_text(f"{digest}  {asset.url}\n")
        (build / COMPLETE).touch()
        _move_into_place(build, target)
    finally:
        if build.exists():
            shutil.rmtree(build, ignore_errors=True)
    return target


def _extract(archive: bytes, asset: Asset, dest: Path) -> None:
    """해시를 확인한 압축 파일에서 실행 파일 하나만 꺼낸다. 일반 파일이 아닌 항목은 거절한다."""
    if asset.url.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(archive)) as z:
            info = z.getinfo(asset.member)
            size = info.file_size
            if size > MAX_BINARY_BYTES:
                raise ToolUnavailable(f"{asset.member}이 크기 상한을 넘음 ({size}바이트)")
            data = z.read(info)
    else:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as t:
            member = t.getmember(asset.member)
            if not member.isfile():
                raise ToolUnavailable(f"{asset.member}이 일반 파일이 아님")
            if member.size > MAX_BINARY_BYTES:
                raise ToolUnavailable(f"{asset.member}이 크기 상한을 넘음 ({member.size}바이트)")
            extracted = t.extractfile(member)
            if extracted is None:
                raise ToolUnavailable(f"{asset.member}을 꺼내지 못함")
            data = extracted.read()
    dest.write_bytes(data)
    dest.chmod(0o755)


def _move_into_place(build: Path, target: Path) -> None:
    """다른 작업이 먼저 옮겼으면 그쪽을 쓴다. 완료 표시 없는 자리는 지우고 다시 옮긴다."""
    for _ in range(2):
        try:
            build.rename(target)
            return
        except OSError:
            if (target / COMPLETE).is_file():
                return
            shutil.rmtree(target, ignore_errors=True)
    build.rename(target)


def _remove_stale(parent: Path, now: float | None = None) -> None:
    now = time.time() if now is None else now
    for tmp in parent.glob(".*.tmp"):
        try:
            if now - tmp.stat().st_mtime > STALE_TMP_SECONDS:
                shutil.rmtree(tmp, ignore_errors=True)
        except OSError:
            pass
