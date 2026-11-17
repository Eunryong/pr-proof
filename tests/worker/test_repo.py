"""worker/repo.py: 로컬 git 원격으로 GitHub 없이 확인한다."""

import subprocess
from pathlib import Path

import pytest

from app.worker import repo
from app.worker.repo import GitError


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip


@pytest.fixture
def remote(tmp_path: Path) -> dict:
    """main에 base 커밋, PR 브랜치에 커밋 하나. refs/pull/1/head가 PR 브랜치를 가리킨다."""
    path = tmp_path / "remote"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "uploadpack.allowAnySHA1InWant", "true")
    (path / "app.py").write_text("X = 1\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "base")
    base = _git(path, "rev-parse", "HEAD")
    _git(path, "checkout", "-q", "-b", "feature")
    (path / "app.py").write_text("X = 2\n")
    (path / "new.py").write_text("Y = 3\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "change")
    head = _git(path, "rev-parse", "HEAD")
    _git(path, "update-ref", "refs/pull/1/head", head)
    _git(path, "checkout", "-q", "main")
    return {"url": str(path), "base": base, "head": head}


def test_checkout_creates_worktree_with_changes_diff(tmp_path: Path, remote: dict) -> None:
    data = tmp_path / "data"
    with repo.checkout(data, "o/r", 1, remote["head"], remote["base"], url=remote["url"]) as co:
        assert (co.path / "app.py").read_text() == "X = 2\n"
        diff = (co.path / repo.CHANGES_DIFF).read_text()
        assert "+X = 2" in diff and "new.py" in diff
        worktree = co.path
    assert not worktree.exists()  # 블록을 나가면 지운다
    assert (repo.bare_path(data, "o/r") / "HEAD").is_file()  # bare 캐시는 남는다


def test_second_checkout_reuses_bare_cache(tmp_path: Path, remote: dict) -> None:
    data = tmp_path / "data"
    with repo.checkout(data, "o/r", 1, remote["head"], remote["base"], url=remote["url"]):
        pass
    bare = repo.bare_path(data, "o/r")
    marker = bare / "cache-marker"
    marker.write_text("x")
    with repo.checkout(data, "o/r", 1, remote["head"], remote["base"], url=remote["url"]):
        pass
    assert marker.exists()  # 다시 clone하지 않았다


def test_worktree_is_removed_when_block_raises(tmp_path: Path, remote: dict) -> None:
    data = tmp_path / "data"
    with pytest.raises(RuntimeError):
        with repo.checkout(data, "o/r", 1, remote["head"], remote["base"], url=remote["url"]):
            raise RuntimeError("테스트 실패")
    assert list((data / "worktrees").iterdir()) == []


def test_unknown_pr_is_git_error(tmp_path: Path, remote: dict) -> None:
    with pytest.raises(GitError):
        with repo.checkout(
            tmp_path / "d", "o/r", 99, remote["head"], remote["base"], url=remote["url"]
        ):
            pass
