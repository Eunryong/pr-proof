"""worker/lang/python.py의 .venv 설치 단위 테스트. Docker 없이 돈다."""

import json
import subprocess
from pathlib import Path
from typing import Final

import pytest

from app.sandbox import docker_cli
from app.sandbox.docker_cli import Image, SandboxError
from app.worker.lang import python
from app.worker.lang.python import DependencyInstallError

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
    assert python.pyproject_requirements(text) == ["fastapi>=0.115", "pyyaml", "pytest>=8", "httpx"]


def test_pyproject_without_project_table_has_no_requirements() -> None:
    assert python.pyproject_requirements("[tool.ruff]\nline-length = 100\n") == []


# --- 캐시 키 ------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "requirements.txt").write_text("pytest==8.3.4\n")
    (tmp_path / "app.py").write_text("X = 1\n")
    return tmp_path


def test_cache_key_is_stable(repo: Path) -> None:
    assert python.cache_key(repo, IMAGE) == python.cache_key(repo, IMAGE)


def test_cache_key_ignores_source_changes(repo: Path) -> None:
    before = python.cache_key(repo, IMAGE)
    (repo / "app.py").write_text("X = 2\n")
    assert python.cache_key(repo, IMAGE) == before


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
    before = python.cache_key(repo, IMAGE)
    change(repo)
    assert python.cache_key(repo, IMAGE) != before


@pytest.mark.parametrize(
    "image",
    [
        Image(id="sha256:bbb", python_version="3.11.15"),
        Image(id="sha256:aaa", python_version="3.11.16"),
    ],
    ids=["image-digest", "python-version"],
)
def test_cache_key_changes_with_image(repo: Path, image: Image) -> None:
    assert python.cache_key(repo, image) != python.cache_key(repo, IMAGE)


def test_cache_key_without_dependency_files(tmp_path: Path) -> None:
    assert len(python.cache_key(tmp_path, IMAGE)) == 24


def test_dependency_files_order(tmp_path: Path) -> None:
    for name in ("pyproject.toml", "requirements-dev.txt", "requirements.txt", "setup.cfg"):
        (tmp_path / name).write_text("")
    assert [p.name for p in python.dependency_files(tmp_path)] == [
        "requirements-dev.txt",
        "requirements.txt",
        "pyproject.toml",
    ]


def test_deps_input_translates_pyproject_and_adds_tools(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "requirements.txt").write_text("pytest\n")
    (repo / "pyproject.toml").write_text("[project]\nname='x'\ndependencies=['pyyaml']\n")
    files, args = python.deps_input(repo)
    assert args == [
        "-r", "/tmp/deps/requirements.txt",
        "-r", "/tmp/deps/pyproject-deps.txt",
        *python.TOOL_REQUIREMENTS,
    ]  # fmt: skip
    # 프로젝트 자체는 넘기지 않는다
    assert files == {"requirements.txt": "pytest\n", "pyproject-deps.txt": "pyyaml\n"}


def test_install_container_gets_inputs_on_stdin_and_one_output_mount(
    tmp_path: Path, repo: Path, monkeypatch
) -> None:
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return _completed(0)

    monkeypatch.setattr(docker_cli, "run", fake_run)
    target = python.ensure_venv(repo, IMAGE, cache_dir=tmp_path / "cache")
    ((args, kwargs),) = calls
    mounts = [args[i + 1] for i, a in enumerate(args) if a in ("-v", "--volume", "--mount")]
    assert len(mounts) == 1 and mounts[0].endswith(f":{python.VENV_IN_CONTAINER}")
    assert mounts[0].split(":")[0].startswith(str(tmp_path / "cache" / "."))  # 빈 임시 출력 폴더
    assert "-i" in args and json.loads(kwargs["input"]) == python.deps_input(repo)[0]
    script = args[-1]
    assert "--only-binary :all:" in script and "-r /tmp/deps/requirements.txt" in script
    assert (target / ".complete").is_file()


def test_cache_key_includes_install_method(repo: Path, monkeypatch) -> None:
    before = python.cache_key(repo, IMAGE)
    monkeypatch.setattr(python, "INSTALL_VERSION", "other")
    assert python.cache_key(repo, IMAGE) != before


@pytest.mark.parametrize(
    ("text", "bad"),
    [
        ("pytest==8.3.4\n# comment\n\nrequests>=2 ; python_version>'3'\n", []),
        ("-r requirements.txt\npytest\n", []),
        ("--no-binary :all:\npytest\n", ["--no-binary :all:"]),
        ("-i https://evil.example/simple\n", ["-i https://evil.example/simple"]),
        ("--extra-index-url=https://x\n", ["--extra-index-url=https://x"]),
        ("-e .\n", ["-e ."]),
        ("pkg @ https://example.com/pkg.tar.gz\n", ["pkg @ https://example.com/pkg.tar.gz"]),
        ("git+https://github.com/a/b.git\n", ["git+https://github.com/a/b.git"]),
        ("./local/pkg\n", ["./local/pkg"]),
        ("-r ../outside.txt\n", ["-r ../outside.txt"]),
        ("-r missing.txt\n", ["-r missing.txt"]),
        ("pytest --hash=sha256:abc\n", []),
    ],
    ids=[
        "plain",
        "include",
        "no-binary",
        "index",
        "extra-index",
        "editable",
        "direct-url",
        "vcs",
        "path",
        "include-outside",
        "include-missing",
        "hash",
    ],  # fmt: skip
)
def test_disallowed_lines(text: str, bad: list[str]) -> None:
    files = {"requirements-dev.txt": text, "requirements.txt": "pytest\n"}
    assert python.disallowed_lines(files) == [f"requirements-dev.txt: {b}" for b in bad]


def test_disallowed_requirement_is_not_installed(tmp_path: Path, repo: Path, monkeypatch) -> None:
    (repo / "requirements.txt").write_text("--no-binary :all:\npytest\n")
    monkeypatch.setattr(docker_cli, "run", lambda *a, **k: pytest.fail("설치하면 안 된다"))
    with pytest.raises(DependencyInstallError) as info:
        python.ensure_venv(repo, IMAGE, cache_dir=tmp_path / "cache")
    assert info.value.kind == "disallowed" and "--no-binary :all:" in info.value.detail
    assert list((tmp_path / "cache").iterdir()) == []


def test_ensure_venv_reuses_complete_cache(tmp_path: Path, repo: Path, monkeypatch) -> None:
    target = tmp_path / "cache" / python.cache_key(repo, IMAGE)
    target.mkdir(parents=True)
    (target / ".complete").touch()
    monkeypatch.setattr(docker_cli, "run", lambda *a, **k: pytest.fail("설치하면 안 된다"))
    assert python.ensure_venv(repo, IMAGE, cache_dir=tmp_path / "cache") == target


def test_ensure_venv_failure_leaves_no_partial_cache(
    tmp_path: Path, repo: Path, monkeypatch
) -> None:
    def fail(*args, **kwargs):
        raise SandboxError("pip 실패")

    monkeypatch.setattr(docker_cli, "run", fail)
    with pytest.raises(SandboxError):
        python.ensure_venv(repo, IMAGE, cache_dir=tmp_path / "cache")
    assert list((tmp_path / "cache").iterdir()) == []


# --- 설치 실패 분류 -------------------------------------------------------------

OFFLINE: Final = """WARNING: Retrying (Retry(total=4, connect=None, read=None))
after connection broken by 'NewConnectionError('<pip._vendor.urllib3.connection.HTTPSConnection
object>: Failed to establish a new connection: [Errno -3] Temporary failure in name resolution')'
ERROR: Could not find a version that satisfies the requirement pytest==8.3.4 (from versions: none)
ERROR: No matching distribution found for pytest==8.3.4"""


# pip 24.0 `install -vv --only-binary :all: docopt==0.6.2 pytest==8.3.4`를 python:3.11-slim에서
# 실제로 돌린 출력을 설치 스크립트가 줄인 모양 (끝부분 + 건너뛴 이름). docopt는 소스 배포본만 있다
SOURCE_ONLY: Final = """  Skipping link: not a file: https://pypi.org/simple/docopt/
ERROR: Could not find a version that satisfies the requirement docopt==0.6.2 (from versions: none)
ERROR: No matching distribution found for docopt==0.6.2
No sources permitted for docopt
No sources permitted for pytest"""


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
        (SOURCE_ONLY, "source_only"),
        (OFFLINE + "\nNo sources permitted for pytest", "network"),
        ("", "unknown"),
    ],
    ids=[
        "offline",
        "no-match",
        "resolution-impossible",
        "conflict",
        "invalid",
        "build",
        "source-only",
        "offline-with-sdist",
        "empty",
    ],  # fmt: skip
)
def test_classify_pip_failure(output: str, kind: str) -> None:
    assert python.classify_pip_failure(output) == kind


def test_source_only_packages_only_names_missing_ones() -> None:
    assert python.source_only_packages(SOURCE_ONLY) == ("docopt",)
    assert python.source_only_packages("ERROR: No matching distribution found for nope==1") == ()


def test_source_only_failure_names_packages(tmp_path: Path, repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(docker_cli, "run", lambda *a, **k: _completed(1, SOURCE_ONLY))
    with pytest.raises(DependencyInstallError) as info:
        python.ensure_venv(repo, IMAGE, cache_dir=tmp_path / "cache")
    assert (info.value.kind, info.value.packages) == ("source_only", ("docopt",))


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
        python.ensure_venv(repo, IMAGE, cache_dir=tmp_path / "cache")
    assert info.value.kind == "resolution"
    assert info.value.cache_key == python.cache_key(repo, IMAGE)
    assert list((tmp_path / "cache").iterdir()) == []


@pytest.mark.parametrize("code", sorted(docker_cli.DOCKER_FAILED))
def test_docker_failure_during_install_is_environment_error(
    tmp_path: Path, repo: Path, monkeypatch, code: int
) -> None:
    monkeypatch.setattr(docker_cli, "run", lambda *a, **k: _completed(code, "docker: error"))
    with pytest.raises(SandboxError):
        python.ensure_venv(repo, IMAGE, cache_dir=tmp_path / "cache")
    assert list((tmp_path / "cache").iterdir()) == []
