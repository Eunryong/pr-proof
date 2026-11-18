"""도구 캐시 통합 테스트: 실제 설치 컨테이너로 ast-grep을 받아 실행 컨테이너에서 돌린다."""

import subprocess
from pathlib import Path

import pytest

from app.sandbox import docker_cli, toolcache

pytestmark = pytest.mark.docker


def test_fetches_ast_grep_for_the_docker_platform(tool_cache: Path) -> None:
    path = toolcache.ensure("ast-grep", cache_dir=tool_cache)
    platform = toolcache.docker_platform()
    assert path == tool_cache / "ast-grep" / "0.51.3" / platform
    assert (path / toolcache.COMPLETE).is_file()
    sha = toolcache.TOOLS["ast-grep"].assets[platform].sha256
    assert (path / toolcache.CHECKSUM_FILE).read_text().startswith(sha)
    proc = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "--read-only",
         "-v", f"{path}:/tools/ast-grep:ro", docker_cli.TOOL_IMAGE,
         "/tools/ast-grep/ast-grep", "--version"],
        capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert proc.returncode == 0 and "0.51.3" in proc.stdout, proc.stderr


def test_second_ensure_does_not_fetch(tool_cache: Path) -> None:
    first = toolcache.ensure("ast-grep", cache_dir=tool_cache)

    def no_download(asset: toolcache.Asset, archive: Path) -> None:
        raise AssertionError("다시 받으면 안 된다")

    assert toolcache.ensure("ast-grep", cache_dir=tool_cache, download=no_download) == first
