"""app/worker/model/claude/workdir.py: 모델 전용 작업 폴더. 실제 git으로 확인하고, 모델은 가짜로
바꾼다."""

import os
import subprocess
from pathlib import Path
from typing import Final

import pytest

from app.worker.git import repo
from app.worker.model.claude import workdir

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


def test_hook_settings_register_the_hook_for_every_tool(tmp_path: Path) -> None:
    import json
    import shlex

    settings = json.loads(workdir.hook_settings(tmp_path / "w k", tmp_path / "hook.log", "/py"))
    (entry,) = settings["hooks"]["PreToolUse"]
    assert entry["matcher"] == "*"
    command = shlex.split(entry["hooks"][0]["command"])
    assert command[:2] == ["/py", "-I"] and command[2].endswith("path_hook.py")
    assert command[3:] == [str(tmp_path / "w k"), str(tmp_path / "hook.log")]  # 공백이 든 경로


def test_path_digest_follows_the_content_of_files_and_folders(wt: Path) -> None:
    files = repo.list_files(wt)
    before = {p: workdir.path_digest(wt, p, files) for p in ("src/app.py", "src", ".", "nope.py")}
    assert before["nope.py"] == "-"
    assert workdir.path_digest(wt, "notes.txt", files) == "-"  # 밖을 가리키는 링크
    assert workdir.path_digest(wt, "linked", files) == "-"  # 링크인 폴더
    assert len({before["src/app.py"], before["src"], before["."]}) == 3
    assert {p: workdir.path_digest(wt, p, files) for p in before} == before  # 같으면 같다
    (wt / "run.sh").write_text("#!/bin/sh\necho\n")  # src 밖의 파일만 바꿈
    after = {p: workdir.path_digest(wt, p, files) for p in before}
    assert after["src/app.py"] == before["src/app.py"] and after["src"] == before["src"]
    assert after["."] != before["."]
    (wt / "src" / "app.py").write_text("X = 2\n")
    assert workdir.path_digest(wt, "src/app.py", files) != before["src/app.py"]
    assert workdir.path_digest(wt, "src", files) != before["src"]
