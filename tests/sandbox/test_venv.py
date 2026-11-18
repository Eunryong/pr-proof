"""app/sandbox/venv.py 단위 테스트. Docker 없이 돈다."""

import subprocess
from pathlib import Path
from typing import Final

import pytest

from app.sandbox import docker_cli, venv
from app.sandbox.docker_cli import Image, SandboxError
from app.sandbox.venv import DependencyInstallError

IMAGE: Final = Image(id="sha256:aaa", python_version="3.11.15")


# --- pyproject 의존성 ---------------------------------------------------------


def test_pyproject_requirements_includes_extras_and_skips_self() -> None:
    text = """
[project]
name = "PR_Proof"
dependencies = ["fastapi>=0.115", "pyyaml"]
[project.optional-dependencies]
dev = ["pytest>=8", "pr-proof[extra]"]
extra = ["httpx", "pr.proof >= 1 ; python_version >= '3.11'"]
"""
    assert venv.pyproject_requirements(text) == ["fastapi>=0.115", "pyyaml", "pytest>=8", "httpx"]


def test_pyproject_without_project_table_has_no_requirements() -> None:
    assert venv.pyproject_requirements("[tool.ruff]\nline-length = 100\n") == []


# --- 캐시 키 ------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "requirements.txt").write_text("pytest==8.3.4\n")
    (tmp_path / "app.py").write_text("X = 1\n")
    return tmp_path


def test_cache_key_is_stable(repo: Path) -> None:
    assert venv.cache_key(repo, IMAGE) == venv.cache_key(repo, IMAGE)


def test_cache_key_ignores_source_changes(repo: Path) -> None:
    before = venv.cache_key(repo, IMAGE)
    (repo / "app.py").write_text("X = 2\n")
    assert venv.cache_key(repo, IMAGE) == before


@pytest.mark.parametrize(
    "change",
    [
        lambda r: (r / "requirements.txt").write_text("pytest==8.3.5\n"),
        lambda r: (r / "requirements-dev.txt").write_text("ruff\n"),
        lambda r: (r / "pyproject.toml").write_text("[project]\nname='x'\n"),
    ],
    ids=["dep-version", "new-requirements-file", "new-pyproject"],
)
def test_cache_key_changes_with_dependency_files(repo: Path, change) -> None:
    before = venv.cache_key(repo, IMAGE)
    change(repo)
    assert venv.cache_key(repo, IMAGE) != before


@pytest.mark.parametrize(
    "image",
    [
        Image(id="sha256:bbb", python_version="3.11.15"),
        Image(id="sha256:aaa", python_version="3.11.16"),
    ],
    ids=["image-digest", "python-version"],
)
def test_cache_key_changes_with_image(repo: Path, image: Image) -> None:
    assert venv.cache_key(repo, image) != venv.cache_key(repo, IMAGE)


def test_cache_key_without_dependency_files(tmp_path: Path) -> None:
    assert len(venv.cache_key(tmp_path, IMAGE)) == 24


def test_dependency_files_order(tmp_path: Path) -> None:
    for name in ("pyproject.toml", "requirements-dev.txt", "requirements.txt", "setup.cfg"):
        (tmp_path / name).write_text("")
    assert [p.name for p in venv.dependency_files(tmp_path)] == [
        "requirements-dev.txt",
        "requirements.txt",
        "pyproject.toml",
    ]


def test_write_deps_translates_pyproject_and_adds_tools(tmp_path: Path) -> None:
    repo, dest = tmp_path / "repo", tmp_path / "deps"
    repo.mkdir()
    dest.mkdir()
    (repo / "requirements.txt").write_text("pytest\n")
    (repo / "pyproject.toml").write_text("[project]\nname='x'\ndependencies=['pyyaml']\n")
    args = venv.write_deps(repo, dest)
    assert args == [
        "-r", "/deps/requirements.txt",
        "-r", "/deps/pyproject-deps.txt",
        *venv.TOOL_REQUIREMENTS,
    ]  # fmt: skip
    assert (dest / "pyproject-deps.txt").read_text() == "pyyaml\n"
    assert not (dest / "pyproject.toml").exists()  # 프로젝트 자체는 넘기지 않는다


def test_ensure_venv_reuses_complete_cache(tmp_path: Path, repo: Path, monkeypatch) -> None:
    target = tmp_path / "cache" / venv.cache_key(repo, IMAGE)
    target.mkdir(parents=True)
    (target / ".complete").touch()
    monkeypatch.setattr(docker_cli, "run", lambda *a, **k: pytest.fail("설치하면 안 된다"))
    assert venv.ensure_venv(repo, IMAGE, cache_dir=tmp_path / "cache") == target


def test_ensure_venv_failure_leaves_no_partial_cache(
    tmp_path: Path, repo: Path, monkeypatch
) -> None:
    def fail(*args, **kwargs):
        raise SandboxError("pip 실패")

    monkeypatch.setattr(docker_cli, "run", fail)
    with pytest.raises(SandboxError):
        venv.ensure_venv(repo, IMAGE, cache_dir=tmp_path / "cache")
    assert list((tmp_path / "cache").iterdir()) == []


# --- 설치 실패 분류 -------------------------------------------------------------

OFFLINE: Final = """WARNING: Retrying (Retry(total=4, connect=None, read=None))
after connection broken by 'NewConnectionError('<pip._vendor.urllib3.connection.HTTPSConnection
object>: Failed to establish a new connection: [Errno -3] Temporary failure in name resolution')'
ERROR: Could not find a version that satisfies the requirement pytest==8.3.4 (from versions: none)
ERROR: No matching distribution found for pytest==8.3.4"""


@pytest.mark.parametrize(
    ("output", "kind"),
    [
        (OFFLINE, "network"),  # "No matching distribution"이 함께 나와도 네트워크가 먼저
        ("ERROR: No matching distribution found for nope==0.0.1", "resolution"),
        ("ERROR: ResolutionImpossible: for help visit https://pip.pypa.io/...", "resolution"),
        (
            "ERROR: Cannot install a and b because these package versions have"
            " conflicting dependencies.",
            "resolution",
        ),
        ("ERROR: Invalid requirement: 'pytest==='", "resolution"),
        ("error: subprocess-exited-with-error\nFailed building wheel for psycopg2", "unknown"),
        ("", "unknown"),
    ],
    ids=["offline", "no-match", "resolution-impossible", "conflict", "invalid", "build", "empty"],
)
def test_classify_pip_failure(output: str, kind: str) -> None:
    assert venv.classify_pip_failure(output) == kind


def _completed(code: int, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=code, stdout="", stderr=stderr)


def test_pip_failure_raises_dependency_error_with_kind(
    tmp_path: Path, repo: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        docker_cli,
        "run",
        lambda *a, **k: _completed(1, "ERROR: No matching distribution found for x"),
    )
    with pytest.raises(DependencyInstallError) as info:
        venv.ensure_venv(repo, IMAGE, cache_dir=tmp_path / "cache")
    assert info.value.kind == "resolution"
    assert info.value.cache_key == venv.cache_key(repo, IMAGE)
    assert list((tmp_path / "cache").iterdir()) == []


@pytest.mark.parametrize("code", sorted(docker_cli.DOCKER_FAILED))
def test_docker_failure_during_install_is_environment_error(
    tmp_path: Path, repo: Path, monkeypatch, code: int
) -> None:
    monkeypatch.setattr(docker_cli, "run", lambda *a, **k: _completed(code, "docker: error"))
    with pytest.raises(SandboxError):
        venv.ensure_venv(repo, IMAGE, cache_dir=tmp_path / "cache")
    assert list((tmp_path / "cache").iterdir()) == []
