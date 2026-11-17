"""app/sandbox.py 단위 테스트. Docker 없이 돈다."""

import subprocess
from pathlib import Path

import pytest

from app.models.sandbox import Image
from app.services import sandbox as sb

IMAGE = Image(id="sha256:aaa", python_version="3.11.15")


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
    assert sb.pyproject_requirements(text) == ["fastapi>=0.115", "pyyaml", "pytest>=8", "httpx"]


def test_pyproject_without_project_table_has_no_requirements() -> None:
    assert sb.pyproject_requirements("[tool.ruff]\nline-length = 100\n") == []


# --- 캐시 키 ------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "requirements.txt").write_text("pytest==8.3.4\n")
    (tmp_path / "app.py").write_text("X = 1\n")
    return tmp_path


def test_cache_key_is_stable(repo: Path) -> None:
    assert sb.cache_key(repo, IMAGE) == sb.cache_key(repo, IMAGE)


def test_cache_key_ignores_source_changes(repo: Path) -> None:
    before = sb.cache_key(repo, IMAGE)
    (repo / "app.py").write_text("X = 2\n")
    assert sb.cache_key(repo, IMAGE) == before


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
    before = sb.cache_key(repo, IMAGE)
    change(repo)
    assert sb.cache_key(repo, IMAGE) != before


@pytest.mark.parametrize(
    "image",
    [
        Image(id="sha256:bbb", python_version="3.11.15"),
        Image(id="sha256:aaa", python_version="3.11.16"),
    ],
    ids=["image-digest", "python-version"],
)
def test_cache_key_changes_with_image(repo: Path, image: Image) -> None:
    assert sb.cache_key(repo, image) != sb.cache_key(repo, IMAGE)


def test_cache_key_without_dependency_files(tmp_path: Path) -> None:
    assert len(sb.cache_key(tmp_path, IMAGE)) == 24


def test_dependency_files_order(tmp_path: Path) -> None:
    for name in ("pyproject.toml", "requirements-dev.txt", "requirements.txt", "setup.cfg"):
        (tmp_path / name).write_text("")
    assert [p.name for p in sb.dependency_files(tmp_path)] == [
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
    args = sb._write_deps(repo, dest)
    assert args == [
        "-r", "/deps/requirements.txt",
        "-r", "/deps/pyproject-deps.txt",
        *sb.TOOL_REQUIREMENTS,
    ]  # fmt: skip
    assert (dest / "pyproject-deps.txt").read_text() == "pyyaml\n"
    assert not (dest / "pyproject.toml").exists()  # 프로젝트 자체는 넘기지 않는다


def test_ensure_venv_reuses_complete_cache(tmp_path: Path, repo: Path, monkeypatch) -> None:
    target = tmp_path / "cache" / sb.cache_key(repo, IMAGE)
    target.mkdir(parents=True)
    (target / ".complete").touch()
    monkeypatch.setattr(sb, "_docker", lambda *a, **k: pytest.fail("설치하면 안 된다"))
    assert sb.ensure_venv(repo, IMAGE, cache_dir=tmp_path / "cache") == target


def test_ensure_venv_failure_leaves_no_partial_cache(
    tmp_path: Path, repo: Path, monkeypatch
) -> None:
    def fail(*args, **kwargs):
        raise sb.SandboxError("pip 실패")

    monkeypatch.setattr(sb, "_docker", fail)
    with pytest.raises(sb.SandboxError):
        sb.ensure_venv(repo, IMAGE, cache_dir=tmp_path / "cache")
    assert list((tmp_path / "cache").iterdir()) == []


# --- 실행 환경 ----------------------------------------------------------------


def test_minimal_env_drops_secrets(monkeypatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = sb.minimal_env()
    assert env["PATH"] == "/usr/bin"
    assert "GH_TOKEN" not in env and "ANTHROPIC_API_KEY" not in env


def test_pythonpath_for_src_layout(tmp_path: Path) -> None:
    assert sb.pythonpath_for(tmp_path) == "/work"
    (tmp_path / "src").mkdir()
    assert sb.pythonpath_for(tmp_path) == "/work/src:/work"


# --- run_in_sandbox: docker exec 대신 로컬 프로세스로 대기·취소 로직만 확인 --------


@pytest.fixture
def fake_exec(monkeypatch):
    """docker exec 자리에 로컬 명령을 띄우고, remove_container는 그 프로세스를 끝낸다."""
    state: dict = {"cmd": None, "procs": [], "removed": []}
    real_popen = subprocess.Popen

    def install(local_cmd: list[str]) -> dict:
        def popen(cmd, **kwargs):
            state["cmd"] = cmd
            proc = real_popen(local_cmd, **kwargs)
            state["procs"].append(proc)
            return proc

        def remove(name: str) -> None:
            state["removed"].append(name)
            for p in state["procs"]:
                p.terminate()

        monkeypatch.setattr(sb.subprocess, "Popen", popen)
        monkeypatch.setattr(sb, "remove_container", remove)
        return state

    return install


def test_run_in_sandbox_builds_exec_command(fake_exec) -> None:
    state = fake_exec(["sh", "-c", "echo out; echo err >&2"])
    r = sb.run_in_sandbox(
        "job-1", [".venv/bin/pytest", "-q"], env={"PYTHONPATH": "/work"}, timeout=5
    )
    assert state["cmd"] == [
        "docker", "exec", "-e", "PYTHONPATH=/work", "job-1",
        "timeout", "5", ".venv/bin/pytest", "-q",
    ]  # fmt: skip
    assert (r.returncode, r.stdout, r.stderr) == (0, "out\n", "err\n")
    assert not r.cancelled and not r.timed_out and state["removed"] == []


def test_run_in_sandbox_returns_failure_code(fake_exec) -> None:
    fake_exec(["sh", "-c", "exit 1"])
    r = sb.run_in_sandbox("job-1", ["x"])
    assert r.returncode == 1 and not r.timed_out


def test_run_in_sandbox_cancels_when_stale(fake_exec) -> None:
    state = fake_exec(["sleep", "30"])
    checks = iter([False, False, True])
    r = sb.run_in_sandbox("job-1", ["x"], is_stale=lambda: next(checks), poll_seconds=0.05)
    assert r.cancelled and r.returncode is None
    assert state["removed"] == ["job-1"]


def test_run_in_sandbox_not_stale_runs_to_end(fake_exec) -> None:
    state = fake_exec(["sh", "-c", "sleep 0.3"])
    r = sb.run_in_sandbox("job-1", ["x"], is_stale=lambda: False, poll_seconds=0.05)
    assert r.returncode == 0 and not r.cancelled and state["removed"] == []


def test_run_in_sandbox_timeout_exit_code(fake_exec) -> None:
    fake_exec(["sh", "-c", f"exit {sb.TIMEOUT_EXIT}"])
    r = sb.run_in_sandbox("job-1", ["x"])
    assert r.timed_out and r.returncode == sb.TIMEOUT_EXIT


def test_run_in_sandbox_host_deadline_removes_container(fake_exec, monkeypatch) -> None:
    monkeypatch.setattr(sb, "HOST_GRACE_SECONDS", 0)
    state = fake_exec(["sleep", "30"])
    r = sb.run_in_sandbox("job-1", ["x"], timeout=0, poll_seconds=0.05)
    assert r.timed_out and not r.cancelled and state["removed"] == ["job-1"]
