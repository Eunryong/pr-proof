"""app/sandbox/toolcache.py 단위 테스트. 받기는 가짜로 바꾸고 Docker 없이 돈다."""

import hashlib
import io
import os
import subprocess
import tarfile
import zipfile
from pathlib import Path
from typing import Final

import pytest

from app.sandbox import toolcache
from app.sandbox.docker_cli import SandboxError
from app.sandbox.toolcache import COMPLETE, Asset, Tool, ToolUnavailable

PLATFORM: Final = "linux-arm64-gnu"
BINARY: Final = b"#!/bin/sh\necho fake\n"


def _zip(binary: bytes = BINARY) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("fake", binary)
        z.writestr("other", b"not extracted")
    return buf.getvalue()


ARCHIVE: Final = _zip()


def _tools(sha256: str = hashlib.sha256(ARCHIVE).hexdigest()) -> dict[str, Tool]:
    asset = Asset("https://example.invalid/fake.zip", sha256, "fake")
    return {"fake": Tool("fake", "fake", "1.0", {"1.0": {PLATFORM: asset}})}


def _tar(member: str, binary: bytes = BINARY, *, link: bool = False) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        info = tarfile.TarInfo(member)
        if link:
            info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
            t.addfile(info)
        else:
            info.size = len(binary)
            t.addfile(info, io.BytesIO(binary))
    return buf.getvalue()


class FakeDownload:
    def __init__(self, content: bytes = ARCHIVE) -> None:
        self.content = content
        self.calls: list[str] = []

    def __call__(self, asset: Asset, archive: Path) -> None:
        self.calls.append(asset.url)
        archive.write_bytes(self.content)


def _ensure(cache: Path, download: FakeDownload, tools: dict[str, Tool] | None = None) -> Path:
    return toolcache.ensure(
        "fake", cache_dir=cache, platform=PLATFORM, download=download, tools=tools or _tools()
    )


def test_fetches_once_and_marks_complete(tmp_path: Path) -> None:
    download = FakeDownload()
    path = _ensure(tmp_path, download)
    assert path == tmp_path / "fake" / "1.0" / PLATFORM
    assert (path / "fake").read_bytes() == BINARY
    assert os.access(path / "fake", os.X_OK)
    assert (path / COMPLETE).is_file()
    assert sorted(p.name for p in path.iterdir()) == [COMPLETE, "SHA256", "fake"]
    assert _ensure(tmp_path, download) == path
    assert len(download.calls) == 1
    assert [p.name for p in path.parent.iterdir()] == [PLATFORM]  # 임시 폴더가 남지 않음


def test_checksum_mismatch_leaves_no_cache_entry(tmp_path: Path) -> None:
    download = FakeDownload(_zip(b"tampered"))
    with pytest.raises(ToolUnavailable, match="해시 불일치"):
        _ensure(tmp_path, download)
    assert list((tmp_path / "fake" / "1.0").iterdir()) == []
    assert _ensure(tmp_path, FakeDownload()).joinpath("fake").read_bytes() == BINARY


def test_download_failure_is_environment_error_and_leaves_nothing(tmp_path: Path) -> None:
    def failing(asset: Asset, archive: Path) -> None:
        archive.write_bytes(b"partial")
        raise SandboxError("network down")

    with pytest.raises(SandboxError):
        _ensure(tmp_path, failing)  # type: ignore[arg-type]
    assert list((tmp_path / "fake" / "1.0").iterdir()) == []


def test_unknown_tool_version_and_platform_are_unavailable(tmp_path: Path) -> None:
    with pytest.raises(ToolUnavailable, match="목록에 없음"):
        toolcache.ensure("semgrep", cache_dir=tmp_path, platform=PLATFORM, download=FakeDownload())
    download = FakeDownload()
    with pytest.raises(ToolUnavailable, match="해시를 고정한 버전이 아님"):
        toolcache.ensure(
            "fake", "9.9", cache_dir=tmp_path, platform=PLATFORM, tools=_tools(),
            download=download,
        )  # fmt: skip
    assert download.calls == []  # 고정하지 않은 버전은 받지도 않는다
    with pytest.raises(ToolUnavailable, match="배포본이 없음"):
        toolcache.ensure(
            "fake", cache_dir=tmp_path, platform="linux-riscv64-gnu", tools=_tools(),
            download=FakeDownload(),
        )  # fmt: skip


def test_existing_complete_entry_wins_a_race(tmp_path: Path) -> None:
    """다른 작업이 먼저 옮긴 완성된 폴더가 있으면 그쪽을 쓴다."""
    target = tmp_path / "fake" / "1.0" / PLATFORM

    def racing(asset: Asset, archive: Path) -> None:
        target.mkdir(parents=True)
        (target / "fake").write_bytes(b"winner")
        (target / COMPLETE).touch()
        archive.write_bytes(ARCHIVE)

    assert _ensure(tmp_path, racing) == target  # type: ignore[arg-type]
    assert (target / "fake").read_bytes() == b"winner"
    assert [p.name for p in target.parent.iterdir()] == [PLATFORM]


def test_incomplete_entry_is_replaced(tmp_path: Path) -> None:
    target = tmp_path / "fake" / "1.0" / PLATFORM
    target.mkdir(parents=True)
    (target / "fake").write_bytes(b"half")
    assert (_ensure(tmp_path, FakeDownload()) / "fake").read_bytes() == BINARY


def test_stale_temp_dirs_are_removed(tmp_path: Path) -> None:
    parent = tmp_path / "fake" / "1.0"
    stale, fresh = parent / f".{PLATFORM}.old.tmp", parent / f".{PLATFORM}.new.tmp"
    stale.mkdir(parents=True)
    fresh.mkdir()
    old = stale.stat().st_mtime - toolcache.STALE_TMP_SECONDS - 1
    os.utime(stale, (old, old))
    _ensure(tmp_path, FakeDownload())
    assert not stale.exists() and fresh.exists()


@pytest.mark.parametrize(
    ("os_name", "arch", "key"),
    [
        ("linux", "arm64", "linux-arm64-gnu"),
        ("linux", "aarch64", "linux-arm64-gnu"),
        ("linux", "amd64", "linux-amd64-gnu"),
        ("Linux", "x86_64", "linux-amd64-gnu"),
    ],
)
def test_platform_key(os_name: str, arch: str, key: str) -> None:
    assert toolcache.platform_key(os_name, arch) == key


def test_platform_key_rejects_unknown_arch() -> None:
    with pytest.raises(ToolUnavailable):
        toolcache.platform_key("linux", "s390x")


def test_registry_pins_every_asset() -> None:
    for tool in toolcache.TOOLS.values():
        assert tool.default in tool.releases
        for release in tool.releases.values():
            assert set(release) == {"linux-arm64-gnu", "linux-amd64-gnu"}
            for asset in release.values():
                assert asset.url.startswith("https://") and len(asset.sha256) == 64


def test_requested_version_has_its_own_folder(tmp_path: Path) -> None:
    """저장소가 고정한 버전은 기본 버전과 다른 폴더다. 두 버전이 함께 캐시에 있다 (3-4)."""
    data = _tar("pkg/fake")
    sha = hashlib.sha256(data).hexdigest()
    url = "https://example.invalid/fake.tar.gz"
    tools = {"fake": Tool("fake", "fake", "1.0", {
        v: {PLATFORM: Asset(url, sha, "pkg/fake")} for v in ("1.0", "2.0")
    })}  # fmt: skip
    download = FakeDownload(data)
    one = toolcache.ensure("fake", cache_dir=tmp_path, platform=PLATFORM, download=download,
                           tools=tools)  # fmt: skip
    two = toolcache.ensure("fake", "2.0", cache_dir=tmp_path, platform=PLATFORM,
                           download=download, tools=tools)  # fmt: skip
    assert (one.parent.name, two.parent.name) == ("1.0", "2.0")
    assert (two / "fake").read_bytes() == BINARY and len(download.calls) == 2


def test_tar_member_that_is_a_link_is_rejected(tmp_path: Path) -> None:
    data = _tar("pkg/fake", link=True)
    tools = {"fake": Tool("fake", "fake", "1.0", {"1.0": {PLATFORM: Asset(
        "https://example.invalid/fake.tar.gz", hashlib.sha256(data).hexdigest(), "pkg/fake",
    )}})}  # fmt: skip
    with pytest.raises(ToolUnavailable, match="일반 파일이 아님"):
        toolcache.ensure("fake", cache_dir=tmp_path, platform=PLATFORM,
                         download=FakeDownload(data), tools=tools)  # fmt: skip
    assert list((tmp_path / "fake" / "1.0").iterdir()) == []


def test_known_versions() -> None:
    assert toolcache.is_known("ruff", "0.17.5") and toolcache.is_known("ruff", "0.17.4")
    assert not toolcache.is_known("ruff", "0.1.0") and not toolcache.is_known("semgrep", "1")


def _tree(*members: tuple[str, bytes | None, int]) -> bytes:
    """(이름, 내용 또는 링크면 None, 모드)로 tar.gz를 만든다."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, content, mode in members:
            info = tarfile.TarInfo(name)
            info.mode = mode
            if content is None:
                info.type, info.linkname = tarfile.SYMTYPE, "python3.11"
                t.addfile(info)
            else:
                info.size = len(content)
                t.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _tree_tools(data: bytes) -> dict[str, Tool]:
    asset = Asset("https://example.invalid/rt.tar.gz", hashlib.sha256(data).hexdigest(), "python/")
    return {"rt": Tool("rt", "bin/python3.11", "1.0", {"1.0": {PLATFORM: asset}})}


def test_tree_tool_extracts_regular_files_and_skips_links(tmp_path: Path) -> None:
    data = _tree(
        ("python/bin/python3.11", BINARY, 0o755),
        ("python/bin/python3", None, 0o777),
        ("python/lib/python3.11/os.py", b"x = 1\n", 0o644),
    )
    path = toolcache.ensure("rt", cache_dir=tmp_path, platform=PLATFORM,
                            download=FakeDownload(data), tools=_tree_tools(data))  # fmt: skip
    assert (path / "bin" / "python3.11").read_bytes() == BINARY
    assert os.access(path / "bin" / "python3.11", os.X_OK)
    assert not os.access(path / "lib" / "python3.11" / "os.py", os.X_OK)
    assert not (path / "bin" / "python3").exists() and not (path / "bin" / "python3").is_symlink()
    assert (path / COMPLETE).is_file() and not (path / "download").exists()


@pytest.mark.parametrize(
    "name",
    ["python/../evil", "/python/abs", "other/file", "python/", f"python/{COMPLETE}",
     "python/SHA256"],
    ids=["dotdot", "absolute", "outside-prefix", "prefix-itself", "complete-marker", "checksum"],
)  # fmt: skip
def test_tree_member_outside_prefix_rejects_whole_archive(tmp_path: Path, name: str) -> None:
    data = _tree(("python/bin/python3.11", BINARY, 0o755), (name, b"x", 0o644))
    with pytest.raises(ToolUnavailable):
        toolcache.ensure("rt", cache_dir=tmp_path, platform=PLATFORM,
                         download=FakeDownload(data), tools=_tree_tools(data))  # fmt: skip
    assert list((tmp_path / "rt" / "1.0").iterdir()) == []


@pytest.mark.parametrize(("max_bytes", "max_files"), [(3, 10), (100, 1)], ids=["bytes", "files"])
def test_tree_size_and_file_count_limits(tmp_path: Path, max_bytes: int, max_files: int) -> None:
    data = _tree(("python/a", b"12", 0o644), ("python/b", b"34", 0o644))
    with pytest.raises(ToolUnavailable, match="상한"):
        toolcache._extract_tree(data, "python/", tmp_path, max_bytes=max_bytes, max_files=max_files)
    toolcache._extract_tree(data, "python/", tmp_path / "ok", max_bytes=4, max_files=2)
    assert (tmp_path / "ok" / "b").read_bytes() == b"34"


def test_fetch_is_curl_over_https_only() -> None:
    argv = toolcache.fetch_argv("https://example.invalid/a.tar.gz", "/out/archive")
    assert argv[0] == "curl" and argv[-1] == "https://example.invalid/a.tar.gz"
    assert argv[argv.index("--proto") + 1] == "=https"
    assert argv[argv.index("--proto-redir") + 1] == "=https"
    assert argv[argv.index("--max-filesize") + 1] == str(toolcache.MAX_ARCHIVE_BYTES)
    for url in ("http://example.invalid/a", "file:///etc/passwd", "-K/etc/passwd"):
        with pytest.raises(ToolUnavailable):
            toolcache.fetch_argv(url, "/out/archive")


def test_fetch_container_gets_only_the_output_folder(tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run(*args: str, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(toolcache.docker_cli, "run", fake_run)
    archive = tmp_path / "out" / "archive"
    archive.parent.mkdir()
    toolcache.fetcher("pipeline-sandbox-unit")(_tools()["fake"].releases["1.0"][PLATFORM], archive)
    (args,) = calls
    assert "--network" not in args  # 받기 컨테이너만 네트워크가 열린다
    mounts = [args[i + 1] for i, a in enumerate(args) if a in ("-v", "--volume", "--mount")]
    assert mounts == [f"{archive.parent.resolve()}:/out"]
    assert "--read-only" in args and f"fsize={toolcache.MAX_ARCHIVE_BYTES}" in args
    image = args.index("pipeline-sandbox-unit")
    assert args[image + 1] == "curl" and args[-1] == "https://example.invalid/fake.zip"


def test_python_runtime_is_pinned_and_unpacked_as_a_tree() -> None:
    tool = toolcache.TOOLS[toolcache.PYTHON]
    assert tool.binary == "bin/python3.11" and tool.default.startswith("3.11.")
    for asset in tool.releases[tool.default].values():
        assert asset.member == "python/" and "install_only_stripped" in asset.url
