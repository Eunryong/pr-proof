"""도구 캐시 (docs/DESIGN.md 2.3, 4.4 도구 준비, 8.1 설치 컨테이너).

도구는 미리 깔지 않는다. 규칙이나 테스트가 그 도구를 쓰기로 정해졌을 때 `ensure`가 받아
(도구, 버전, 플랫폼) 폴더에 두고, 실행 컨테이너는 그 폴더를 읽기 전용으로 마운트한다. 버전은
저장소가 고정한 것을 먼저 쓰고, 없으면 도구의 기본 버전이다. 해시를 고정해 둔 버전만 받는다.
언어의 런타임(Python)도 이 목록의 도구다. 샌드박스 이미지에는 언어가 없다.

받는 일은 네트워크가 열린 받기 컨테이너만 한다. 그 컨테이너는 이미지의 curl로 https URL 하나를
받고, 빈 출력 폴더 하나만 붙인다. 받은 파일은 호스트가 고정해 둔 SHA256과 맞춰 본 뒤에만 풀어
캐시에 넣는다. 임시 폴더에서 만들고 완료 표시를 쓴 다음 제자리로 옮기므로, 중단되거나 동시에
받아도 캐시에는 완성된 폴더만 남는다.
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
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Final

from app.worker.sandbox import docker_cli
from app.worker.sandbox.safe_read import read_regular_file

TOOL_CACHE: Final = Path.home() / ".cache" / "pipeline" / "tools"
COMPLETE: Final = ".complete"
CHECKSUM_FILE: Final = "SHA256"
FETCH_TIMEOUT: Final = 300
MAX_ARCHIVE_BYTES: Final = 64 * 1024 * 1024
MAX_BINARY_BYTES: Final = 256 * 1024 * 1024
# 폴더째 푸는 도구(런타임)의 상한. Python 3.11 런타임은 약 90MB, 파일 4천 개다
MAX_TREE_BYTES: Final = 512 * 1024 * 1024
MAX_TREE_FILES: Final = 20_000
# 이보다 오래된 임시 폴더는 중단된 설치가 남긴 것이다. 진행 중인 설치는 FETCH_TIMEOUT 안에 끝난다
STALE_TMP_SECONDS: Final = FETCH_TIMEOUT * 4
# 샌드박스 이미지(docker/sandbox/Dockerfile)가 glibc 기반이라 gnu 빌드를 받는다
LIBC: Final = "gnu"
_ARCH: Final[Mapping[str, str]] = {
    "arm64": "arm64",
    "aarch64": "arm64",
    "amd64": "amd64",
    "x86_64": "amd64",
}


@dataclass(frozen=True)
class Asset:
    url: str
    sha256: str  # 받은 압축 파일의 해시. 처음 한 번 받아 계산하고 GitHub의 digest와 맞춰 봤다
    # 압축 파일(zip 또는 tar.gz) 안의 실행 파일 경로. `/`로 끝나면 그 폴더 아래의 일반 파일을 모두
    # 푼다(tar.gz만. 런타임처럼 실행 파일 하나가 아닌 도구)
    member: str


@dataclass(frozen=True)
class Tool:
    name: str
    binary: str  # 캐시 폴더 안의 실행 파일 경로
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


def _python(
    release: str, versions: Mapping[str, Mapping[str, tuple[str, str]]]
) -> Mapping[str, Mapping[str, Asset]]:
    """python-build-standalone의 `install_only_stripped` 배포본. 압축 파일의 `python/` 아래를
    푼다."""
    base = f"https://github.com/astral-sh/python-build-standalone/releases/download/{release}"
    return {
        version: {
            platform: Asset(
                f"{base}/cpython-{version}%2B{release}-{triple}-install_only_stripped.tar.gz",
                sha256,
                "python/",
            )
            for platform, (triple, sha256) in sums.items()
        }
        for version, sums in versions.items()
    }


_ARM: Final = "aarch64-unknown-linux-gnu"
_X86: Final = "x86_64-unknown-linux-gnu"
PYTHON: Final = "python"

# 설치할 수 있는 도구와 버전은 이 목록뿐이다 (8.1). 모델이나 PR이 다른 것을 받게 할 수 없다.
# ruff의 기본 버전은 v1 샌드박스 이미지에 들어 있던 버전이다. 저장소가 목록 밖의 버전을 고정하면
# 그 도구를 쓰는 규칙은 "검사하지 못함 (도구를 준비하지 못함)"이다 (4.4).
# Python 런타임은 버전 하나다. 3단계 전 샌드박스 이미지(python:3.11-slim, 3.11.15)와 같은 3.11이다.
# 저장소가 요구하는 버전(requires-python)으로 고르는 일은 아직 하지 않는다 (DESIGN 11.1)
TOOLS: Final[Mapping[str, Tool]] = {
    PYTHON: Tool(
        name=PYTHON,
        binary="bin/python3.11",
        default="3.11.18",
        releases=_python("20261026", {
            "3.11.18": {
                "linux-arm64-gnu": (
                    _ARM, "ecf28575cd67fa7a53c7bf2355bae5cff2c496efeb51780d50a8b54441cdce74"
                ),
                "linux-amd64-gnu": (
                    _X86, "17c4978a36c496dc53c08899ef8dad1ad2734f6325f08d6033a1e66419e0af26"
                ),
            },
        }),
    ),
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


def fetch_argv(url: str, dest: str) -> list[str]:
    """받기 컨테이너의 curl 명령. https만 받고, 다른 곳으로 넘겨져도 https만 따라간다."""
    if not url.startswith("https://"):
        raise ToolUnavailable(f"https URL만 받는다: {url}")
    return [
        "curl", "--proto", "=https", "--proto-redir", "=https", "--fail", "--silent",
        "--show-error", "--location", "--max-filesize", str(MAX_ARCHIVE_BYTES),
        "--max-time", str(FETCH_TIMEOUT - 30), "--output", dest, url,
    ]  # fmt: skip


def docker_download(asset: Asset, archive: Path, *, image: str) -> None:
    """받기 컨테이너(image의 curl)에서 받는다. 붙이는 호스트 경로는 archive가 든 빈 폴더 하나뿐이다.

    서버가 크기를 알리지 않아도 넘지 못하게 커널의 파일 크기 상한(`--ulimit fsize`)을 같이 건다.
    """
    out = archive.parent
    proc = docker_cli.run(
        "run", "--rm", "--read-only", *docker_cli.limits(),
        "--ulimit", f"fsize={MAX_ARCHIVE_BYTES}",
        "--user", docker_cli.user(), "-e", "HOME=/tmp",
        "-v", f"{out.resolve()}:/out",
        image, *fetch_argv(asset.url, f"/out/{archive.name}"),
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
    download: Download,
    tools: Mapping[str, Tool] = TOOLS,
) -> Path:
    """그 도구의 캐시 폴더(실행 파일 `Tool.binary`가 든 곳). 없으면 받아서 만든다.

    version이 없으면 도구의 기본 버전이다. 목록에 없는 도구·버전, 이 플랫폼의 배포본이 없음, 해시
    불일치는 ToolUnavailable. 받기 자체의 실패(네트워크, docker)는 SandboxError로 환경 오류다.
    download는 운영에서 `fetcher(이미지 태그)`다.
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
        download(asset, archive)
        data = read_regular_file(archive, max_bytes=MAX_ARCHIVE_BYTES)
        if data is None:
            raise docker_cli.SandboxError(f"받은 파일을 읽지 못함: {asset.url}")
        digest = hashlib.sha256(data).hexdigest()
        if digest != asset.sha256:
            raise ToolUnavailable(
                f"{name} {version} 해시 불일치: 받은 {digest}, 고정한 {asset.sha256}"
            )
        shutil.rmtree(download_dir)
        if asset.member.endswith("/"):
            _extract_tree(data, asset.member, build)
        else:
            _extract(data, asset, build / tool.binary)
        (build / CHECKSUM_FILE).write_text(f"{digest}  {asset.url}\n")
        (build / COMPLETE).touch()
        _move_into_place(build, target)
    finally:
        if build.exists():
            shutil.rmtree(build, ignore_errors=True)
    return target


def fetcher(image: str) -> Download:
    """샌드박스 이미지 image의 받기 컨테이너로 받는 Download. 이미지는 먼저 빌드돼 있어야 한다."""
    return partial(docker_download, image=image)


def _extract(archive: bytes, asset: Asset, dest: Path) -> None:
    """해시를 확인한 압축 파일에서 실행 파일 하나만 꺼낸다. 일반 파일이 아닌 항목은 거절한다."""
    if asset.member.endswith("/"):
        raise ValueError(f"폴더째 푸는 도구는 _extract_tree를 쓴다: {asset.member}")
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


def _extract_tree(
    archive: bytes,
    prefix: str,
    dest: Path,
    *,
    max_bytes: int = MAX_TREE_BYTES,
    max_files: int = MAX_TREE_FILES,
) -> None:
    """해시를 확인한 tar.gz에서 prefix 아래의 일반 파일만 dest 아래로 푼다.

    심볼릭 링크·하드 링크·장치·폴더 같은 일반 파일이 아닌 항목은 만들지 않는다(런타임의 링크는
    `python3` → `python3.11` 같은 별칭이라 없어도 돈다). 풀 일반 파일 중 prefix 밖이거나 경로가
    빠져나가는 것(`..`, 절대 경로)이 하나라도 있으면 압축 파일 전체를 거절한다. 크기와 파일 수에
    상한이 있다.
    """
    total = count = 0
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as t:
        for member in t:
            if not member.isfile():
                continue
            rel = _tree_path(member.name, prefix)
            total += member.size
            count += 1
            if total > max_bytes or count > max_files:
                raise ToolUnavailable(f"{prefix} 아래가 크기·파일 수 상한을 넘음")
            extracted = t.extractfile(member)
            if extracted is None:
                raise ToolUnavailable(f"{member.name}을 꺼내지 못함")
            path = dest / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(extracted.read())
            path.chmod(0o755 if member.mode & 0o111 else 0o644)


def _tree_path(name: str, prefix: str) -> PurePosixPath:
    """압축 파일 항목 이름의 prefix 아래 상대 경로. 빠져나가거나 prefix 밖이면 ToolUnavailable."""
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not name.startswith(prefix):
        raise ToolUnavailable(f"압축 파일의 항목이 {prefix} 밖을 가리킴: {name!r}")
    rel = PurePosixPath(name[len(prefix) :])
    if not rel.parts or rel.parts[0] in (COMPLETE, CHECKSUM_FILE):
        raise ToolUnavailable(f"압축 파일의 항목 이름을 쓸 수 없음: {name!r}")
    return rel


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
