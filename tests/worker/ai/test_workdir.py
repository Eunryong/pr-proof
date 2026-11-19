"""worker/ai/workdir.py: 모델 전용 작업 폴더. 실제 git으로 확인하고, 모델은 가짜로 바꾼다."""

import os
import subprocess
from pathlib import Path
from typing import Final

import pytest

from app.worker import repo
from app.worker.ai import workdir
from app.worker.ai.claude_cli import AIErrorKind, ClaudeResult

SECRET: Final = "outside-secret\n"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    path = tmp_path / "outside"
    path.mkdir()
    (path / "secret.txt").write_text(SECRET)
    return path


@pytest.fixture
def wt(tmp_path: Path, outside: Path) -> Path:
    """일반 파일 몇 개와, 밖을 가리키는 링크 파일·링크 폴더가 커밋된 worktree."""
    path = tmp_path / "wt"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    repo.record_git_dir(path)
    (path / "src").mkdir()
    (path / "src" / "app.py").write_text("X = 1\n")
    (path / "src" / "data.bin").write_bytes(b"\xff\xfe\x00binary")
    (path / "run.sh").write_text("#!/bin/sh\n")
    (path / "run.sh").chmod(0o755)
    (path / ".gitignore").write_text("build/\n")
    (path / "notes.txt").symlink_to(outside / "secret.txt")
    (path / "linked").symlink_to(outside, target_is_directory=True)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _ok(tool_uses: int = 0) -> ClaudeResult:
    return ClaudeResult(
        ok=True, error=None, cost_usd=0.01, num_turns=1, permission_denials=(), text="고침",
        tool_uses=tool_uses,
    )  # fmt: skip


def _model(edit, tool_uses: int = 0, hook_lines: int = 0):
    """모델 폴더에서 edit(work)를 하고, 훅 기록을 hook_lines줄 남기는 가짜 run_claude."""
    seen: dict[str, object] = {}

    def run(work: Path, prompt: str, *, settings: Path, **kwargs) -> ClaudeResult:
        seen.update(work=work, settings=settings, files=_files(work), kwargs=kwargs)
        hook_log = settings.parent / "hook.log"
        hook_log.write_text('{"tool": "Read", "denied": null}\n' * hook_lines)
        edit(work)
        return _ok(tool_uses)

    run.seen = seen
    return run


def _files(root: Path) -> list[str]:
    return sorted(
        str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() or p.is_symlink()
    )


def test_folder_has_regular_files_only(wt: Path, outside: Path) -> None:
    os.mkfifo(wt / "pipe")
    (wt / "build").mkdir()
    (wt / "build" / "out.txt").write_text("ignored\n")
    (wt / ".pipeline-out").mkdir()
    (wt / ".pipeline-out" / "ruff.toml").write_text("")
    (wt / "new.py").write_text("NEW = 1\n")  # 무시되지 않은 새 파일 (앞 회차의 모델이 만든 것)
    (wt / "big.bin").write_bytes(b"0" * (workdir.MAX_FILE_BYTES + 1))

    with workdir.model_folder(wt, repo.list_files(wt)) as folder:
        assert _files(folder.work) == [".gitignore", "new.py", "run.sh", "src/app.py",
                                       "src/data.bin"]  # fmt: skip
        assert not any(p.is_symlink() for p in folder.work.rglob("*"))
        assert os.access(folder.work / "run.sh", os.X_OK)
        assert not os.access(folder.work / "src" / "app.py", os.X_OK)
        # 설정, 훅 기록, 스냅샷 저장소는 모델의 폴더 밖에 있다
        assert folder.work not in folder.settings.parents
        assert not (folder.work / ".git").exists()
        assert folder.patch() == b""
        root = folder.work.parent
    assert not root.exists()


def test_files_under_a_linked_folder_and_unsafe_paths_are_not_copied(
    wt: Path, outside: Path, tmp_path: Path
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    unsafe = ["linked/secret.txt", "notes.txt", "../outside/secret.txt", ".git/config"]
    paths = ["src/app.py", *unsafe, str(outside / "secret.txt"), "src/../src/app.py", "missing.py"]
    skipped = workdir.copy_regular_files(wt, paths, work)
    assert _files(work) == ["src/app.py"]
    assert skipped == paths[1:]


def test_a_file_swapped_for_a_link_after_checkout_is_not_copied(wt: Path, outside: Path) -> None:
    """샌드박스에서 돈 PR 코드가 추적 중인 파일을 링크로 바꿔 둔 경우."""
    (wt / "src" / "app.py").unlink()
    (wt / "src" / "app.py").symlink_to(outside / "secret.txt")
    with workdir.model_folder(wt, repo.list_files(wt)) as folder:
        assert "src/app.py" not in _files(folder.work)


def test_changes_are_applied_to_the_worktree_as_a_diff(wt: Path) -> None:
    def edit(work: Path) -> None:
        (work / "src" / "app.py").write_text("LIMIT = 1\nX = LIMIT\n")
        (work / "src" / "limits.py").write_text("LIMIT = 1\n")
        (work / "src" / "data.bin").write_bytes(b"\x00\xff changed")

    model = _model(edit, tool_uses=3, hook_lines=3)
    result = workdir.run_in_model_folder(wt, "고쳐라", run=model, model="haiku", max_budget_usd=1)

    assert result.ok and model.seen["kwargs"] == {"model": "haiku", "max_budget_usd": 1}
    assert model.seen["work"] != wt and "notes.txt" not in model.seen["files"]
    assert (wt / "src" / "app.py").read_text() == "LIMIT = 1\nX = LIMIT\n"
    assert (wt / "src" / "limits.py").read_text() == "LIMIT = 1\n"
    assert (wt / "src" / "data.bin").read_bytes() == b"\x00\xff changed"
    # worktree의 링크는 그대로이고, 범위 검사가 보는 status에 바뀐 파일이 보인다
    assert (wt / "notes.txt").is_symlink()
    assert " M src/app.py" in repo.status(wt) and "?? src/limits.py" in repo.status(wt)
    # 적용한 diff가 결과에 남는다. 같은 diff를 처음 상태에 다시 적용하면 같은 결과가 된다
    assert b"src/limits.py" in result.applied and b"LIMIT = 1" in result.applied


def test_no_change_applies_nothing(wt: Path) -> None:
    result = workdir.run_in_model_folder(wt, "p", run=_model(lambda work: None))
    assert result.applied == b""
    assert result.ok and repo.status(wt) == ""


def test_changes_are_discarded_when_the_hook_did_not_run(wt: Path) -> None:
    """모델이 도구를 불렀는데 훅 기록이 그보다 적으면 훅 없이 돈 것이다."""
    model = _model(lambda work: (work / "src" / "app.py").write_text("X = 2\n"),
                   tool_uses=2, hook_lines=1)  # fmt: skip
    result = workdir.run_in_model_folder(wt, "p", run=model)
    assert (result.ok, result.error) == (False, AIErrorKind.TOOLS)
    assert "훅" in result.detail and result.cost_usd == 0.01
    assert (wt / "src" / "app.py").read_text() == "X = 1\n"


def test_nothing_is_written_through_a_link_in_the_worktree(wt: Path, outside: Path) -> None:
    """모델 폴더에서는 진짜 폴더인 경로가 worktree에서는 밖을 가리키는 링크다."""

    def edit(work: Path) -> None:
        (work / "src" / "app.py").write_text("X = 2\n")
        (work / "linked").mkdir()
        (work / "linked" / "planted.txt").write_text("planted\n")

    result = workdir.run_in_model_folder(wt, "p", run=_model(edit))
    assert (result.ok, result.error) == (False, AIErrorKind.APPLY)
    assert _files(outside) == ["secret.txt"]
    assert (wt / "src" / "app.py").read_text() == "X = 1\n"  # 하나라도 안 되면 전부 적용하지 않는다


def test_a_link_in_the_worktree_is_not_overwritten(wt: Path, outside: Path) -> None:
    result = workdir.run_in_model_folder(
        wt, "p", run=_model(lambda work: (work / "notes.txt").write_text("overwritten\n"))
    )
    assert result.error == AIErrorKind.APPLY
    assert (outside / "secret.txt").read_text() == SECRET and (wt / "notes.txt").is_symlink()


def test_a_tool_list_mismatch_is_returned_as_is(wt: Path) -> None:
    failure = ClaudeResult(
        ok=False, error=AIErrorKind.TOOLS, cost_usd=None, num_turns=0, permission_denials=(),
        text="", detail="도구 목록이 기대와 다름",
    )  # fmt: skip
    result = workdir.run_in_model_folder(wt, "p", run=lambda *a, **k: failure)
    assert result is failure


def test_hook_settings_register_the_hook_for_every_tool(tmp_path: Path) -> None:
    import json
    import shlex

    settings = json.loads(workdir.hook_settings(tmp_path / "w k", tmp_path / "hook.log", "/py"))
    (entry,) = settings["hooks"]["PreToolUse"]
    assert entry["matcher"] == "*"
    command = shlex.split(entry["hooks"][0]["command"])
    assert command[:2] == ["/py", "-I"] and command[2].endswith("path_hook.py")
    assert command[3:] == [str(tmp_path / "w k"), str(tmp_path / "hook.log")]  # 공백이 든 경로
