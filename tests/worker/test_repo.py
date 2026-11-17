"""worker/repo.py: 로컬 git 원격으로 GitHub 없이 확인한다."""

import subprocess
from pathlib import Path
from typing import Final

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


BASE_APP: Final = "A = 1\nB = 2\nC = 3\nD = 4\nE = 5\n"


def _advanced_remote(tmp_path: Path, main_change: dict[str, str]) -> dict:
    """PR 브랜치가 갈라진 뒤 main이 앞서 나간 원격. PR은 app.py의 2번째 줄만 바꾼다."""
    path = tmp_path / "remote"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "uploadpack.allowAnySHA1InWant", "true")
    (path / "app.py").write_text(BASE_APP)
    (path / "other.py").write_text("Z = 0\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "base")
    fork = _git(path, "rev-parse", "HEAD")
    _git(path, "checkout", "-q", "-b", "feature")
    (path / "app.py").write_text(BASE_APP.replace("B = 2", "B = 20"))
    _git(path, "commit", "-q", "-am", "pr")
    head = _git(path, "rev-parse", "HEAD")
    _git(path, "update-ref", "refs/pull/1/head", head)
    _git(path, "checkout", "-q", "main")
    for name, text in main_change.items():
        (path / name).write_text(text)
    _git(path, "commit", "-q", "-am", "main moves on")
    return {"url": str(path), "base": _git(path, "rev-parse", "HEAD"), "head": head, "fork": fork}


@pytest.mark.parametrize(
    "main_change",
    [
        {"other.py": "Z = 99\n"},
        {"app.py": BASE_APP.replace("E = 5", "E = 50")},
    ],
    ids=["other-file", "same-file-other-line"],
)
def test_base_ahead_counts_only_pr_lines(tmp_path: Path, main_change: dict[str, str]) -> None:
    """base 브랜치가 앞서 나가도 PR이 바꾼 줄은 갈라진 커밋 기준으로 센다.

    base의 지금 커밋과 바로 비교하면 main에서 바뀐 줄(other.py, app.py 5번째 줄)이
    PR의 변경으로 잡혀, 그 줄의 위반까지 PR 작성자의 것으로 보이고 자동 수정 대상이 된다.
    """
    from app.models.diff import added_lines

    remote = _advanced_remote(tmp_path, main_change)
    data = tmp_path / "data"
    with repo.checkout(data, "o/r", 1, remote["head"], remote["base"], url=remote["url"]) as co:
        assert co.merge_base == remote["fork"] != co.base_sha
        pr_only = {"app.py": frozenset({2})}
        assert added_lines((co.path / repo.CHANGES_DIFF).read_text()) == pr_only
        assert added_lines(repo.working_diff(co.path, co.merge_base)) == pr_only
        # 수정을 적용한 뒤 다시 세도 같은 기준이다
        (co.path / "app.py").write_text(BASE_APP.replace("B = 2", "B = 21"))
        assert added_lines(repo.working_diff(co.path, co.merge_base)) == pr_only
        # 고치기 전 동작: base의 지금 커밋과 비교하면 main의 변경이 섞인다
        assert added_lines(repo.working_diff(co.path, co.base_sha)) != pr_only
