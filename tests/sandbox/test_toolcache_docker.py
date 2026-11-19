"""도구 캐시 통합 테스트: 최소 이미지의 받기 컨테이너(curl)로 도구와 Python 런타임을 받아 실행
컨테이너에서 돌린다."""

import subprocess
from pathlib import Path

import pytest

from app.sandbox import toolcache

pytestmark = pytest.mark.docker


def _run(image: str, options: list[str], argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "--read-only", *options, image, *argv],
        capture_output=True, text=True, timeout=60,
    )  # fmt: skip


def test_image_has_no_language_runtime(sandbox_image: str) -> None:
    """이미지는 언어와 무관하다 (2.3, 8.1). Python도 ruff도 없고 받기에 쓰는 curl만 있다."""
    proc = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", sandbox_image, "sh", "-c",
         "for c in python python3 pip ruff ast-grep; do command -v $c && exit 1; done;"
         " command -v curl && command -v timeout"],
        capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_fetches_ast_grep_for_the_docker_platform(tool_cache: Path, sandbox_image: str) -> None:
    path = toolcache.ensure(
        "ast-grep", cache_dir=tool_cache, download=toolcache.fetcher(sandbox_image)
    )
    platform = toolcache.docker_platform()
    assert path == tool_cache / "ast-grep" / "0.51.3" / platform
    assert (path / toolcache.COMPLETE).is_file()
    sha = toolcache.TOOLS["ast-grep"].releases["0.51.3"][platform].sha256
    assert (path / toolcache.CHECKSUM_FILE).read_text().startswith(sha)
    proc = _run(
        sandbox_image, ["-v", f"{path}:/tools/ast-grep:ro"], ["/tools/ast-grep/ast-grep", "-V"]
    )
    assert proc.returncode == 0 and "0.51.3" in proc.stdout, proc.stderr


def test_second_ensure_does_not_fetch(tool_cache: Path, sandbox_image: str) -> None:
    first = toolcache.ensure(
        "ast-grep", cache_dir=tool_cache, download=toolcache.fetcher(sandbox_image)
    )

    def no_download(asset: toolcache.Asset, archive: Path) -> None:
        raise AssertionError("다시 받으면 안 된다")

    assert toolcache.ensure("ast-grep", cache_dir=tool_cache, download=no_download) == first


@pytest.mark.parametrize("version", ["0.17.5", "0.17.4"])
def test_fetches_pinned_ruff_tar_gz(tool_cache: Path, sandbox_image: str, version: str) -> None:
    """ruff는 tar.gz다. 기본 버전과 저장소가 고정할 수 있는 다른 버전이 각자 폴더에 들어간다."""
    path = toolcache.ensure(
        "ruff", version, cache_dir=tool_cache, download=toolcache.fetcher(sandbox_image)
    )
    assert path.parent.name == version
    proc = _run(sandbox_image, ["-v", f"{path}:/tools/ruff:ro"], ["/tools/ruff/ruff", "--version"])
    assert proc.returncode == 0 and proc.stdout.strip() == f"ruff {version}", proc.stderr


def test_python_runtime_runs_read_only_without_its_links(
    tool_cache: Path, sandbox_image: str
) -> None:
    """런타임은 폴더째 받아 링크 없이 푼다. 읽기 전용으로 붙여도 표준 라이브러리와 venv가 돈다."""
    path = toolcache.ensure(
        toolcache.PYTHON, cache_dir=tool_cache, download=toolcache.fetcher(sandbox_image)
    )
    tool = toolcache.TOOLS[toolcache.PYTHON]
    assert path.parent.name == tool.default
    assert not any(p.is_symlink() for p in path.rglob("*"))
    code = "import sys, ssl, sqlite3, json, venv; print(sys.version.split()[0])"
    proc = _run(
        sandbox_image,
        ["-v", f"{path}:/tools/python:ro", "-e", "PYTHONDONTWRITEBYTECODE=1"],
        [f"/tools/python/{tool.binary}", "-I", "-c", code],
    )
    assert proc.returncode == 0 and proc.stdout.strip() == tool.default, proc.stderr
