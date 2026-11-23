"""app/worker/git/repo.py의 PR 사실 읽기 (4.5 끝): 커밋과 메시지, 바뀐 파일. 로컬 git 저장소로
본다."""

import subprocess
from pathlib import Path

import pytest

from app.worker.git import repo
from app.worker.git.repo import GitError


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip


def _commit(path: Path, name: str, message: str) -> str:
    (path / name).write_text(message[:20] + "\n")
    _git(path, "add", "-A")
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q",
         "--cleanup=verbatim", "-F", "-"],
        cwd=path, input=message.encode(), check=True, capture_output=True,
    )  # fmt: skip
    return _git(path, "rev-parse", "HEAD")


@pytest.fixture
def work(tmp_path: Path) -> Path:
    path = tmp_path / "wt"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _commit(path, "base.txt", "base")
    repo.record_git_dir(path)
    return path


def test_commits_come_newest_first_with_parents_and_whole_messages(work: Path) -> None:
    base = _git(work, "rev-parse", "HEAD")
    first = _commit(work, "a.txt", "feat: 하나\n\n본문 줄")
    second = _commit(work, "b.txt", "update")
    commits, more = repo.pr_commits(work, base, second, limit=10, message_bytes=4096)
    assert [c.sha for c in commits] == [second, first] and not more
    assert commits[1].message == "feat: 하나\n\n본문 줄" and commits[1].parents == (base,)
    assert repo.changed_paths(work, base, second) == ("a.txt", "b.txt")


def test_a_message_cannot_pretend_to_be_another_commit(work: Path) -> None:
    """메시지 안의 구분자나 가짜 객체 머리는 다른 커밋이 되지 못한다 (객체 크기로 읽는다)."""
    base = _git(work, "rev-parse", "HEAD")
    forged = f"feat: x\n\x1e{'e' * 40} {'d' * 40}\n가짜\n{'e' * 40} commit 5\nhello\n"
    head = _commit(work, "a.txt", forged)
    commits, _ = repo.pr_commits(work, base, head, limit=10, message_bytes=4096)
    (commit,) = commits
    assert commit.sha == head and commit.message == forged


@pytest.mark.parametrize("count,more", [(3, False), (4, True)])
def test_commit_limit_reports_that_more_were_left(work: Path, count: int, more: bool) -> None:
    base = _git(work, "rev-parse", "HEAD")
    head = base
    for i in range(count):
        head = _commit(work, f"f{i}.txt", f"feat: {i}")
    commits, left = repo.pr_commits(work, base, head, limit=3, message_bytes=4096)
    assert len(commits) == 3 and left is more


def test_huge_message_is_read_only_up_to_the_room_and_marked(work: Path) -> None:
    base = _git(work, "rev-parse", "HEAD")
    head = _commit(work, "a.txt", "feat: big\n\n" + "x" * 200_000)
    (commit,), _ = repo.pr_commits(work, base, head, limit=5, message_bytes=10)
    assert commit.cut and len(commit.message.encode()) < 200_000


def test_merge_commits_have_two_parents(work: Path) -> None:
    base = _git(work, "rev-parse", "HEAD")
    _git(work, "checkout", "-q", "-b", "side")
    side = _commit(work, "s.txt", "side")
    _git(work, "checkout", "-q", "main")
    _commit(work, "m.txt", "feat: main")
    _git(work, "merge", "-q", "--no-ff", "-m", "Merge branch 'side'", "side")
    head = _git(work, "rev-parse", "HEAD")
    commits, _ = repo.pr_commits(work, base, head, limit=10, message_bytes=4096)
    merge = next(c for c in commits if c.sha == head)
    assert len(merge.parents) == 2 and merge.parents[1] == side


def test_no_commits_is_empty(work: Path) -> None:
    head = _git(work, "rev-parse", "HEAD")
    assert repo.pr_commits(work, head, head, limit=5, message_bytes=10) == ([], False)


def test_missing_git_dir_record_is_a_git_error(tmp_path: Path) -> None:
    with pytest.raises(GitError):
        repo.pr_commits(tmp_path, "a" * 40, "b" * 40, limit=5, message_bytes=10)
