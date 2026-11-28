"""app/worker/git/repo.py: 로컬 git 원격으로 GitHub 없이 확인한다."""

import subprocess
from pathlib import Path
from typing import Final

import pytest

from app.worker.git import repo
from app.worker.git.repo import GitError


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
    from app.worker.core.diff_pure import added_lines

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


def test_commit_and_push_stages_only_allowed_paths(tmp_path: Path, remote: dict) -> None:
    data = tmp_path / "data"
    with repo.checkout(data, "o/r", 1, remote["head"], remote["base"], url=remote["url"]) as co:
        (co.path / "app.py").write_text("X = 3\n")  # 허용: 위반을 고친 파일
        (co.path / "new.py").write_text("Y = 4\n")  # 바뀌었지만 허용 목록에 없음
        (co.path / "stray.txt").write_text("test output\n")  # 테스트가 남긴 파일
        sha = repo.commit_and_push(
            co.path, ["app.py"], "[pipeline] lint", "pipeline/pr-1-fix-lint",
            ("me", "42+me@users.noreply.github.com"),
        )  # fmt: skip
    pushed = _git(Path(remote["url"]), "rev-parse", "refs/heads/pipeline/pr-1-fix-lint")
    assert pushed == sha
    files = _git(Path(remote["url"]), "show", "--name-only", "--format=", sha).split()
    assert files == ["app.py"]  # .pipeline-out, 다른 파일은 섞이지 않는다
    author = _git(Path(remote["url"]), "show", "-s", "--format=%an <%ae>", sha)
    assert author == "me <42+me@users.noreply.github.com>"
    parent = _git(Path(remote["url"]), "rev-parse", f"{sha}^")
    assert parent == remote["head"]  # 원본 PR의 커밋 위에 쌓인다


def test_commit_ignores_user_hooks(tmp_path: Path, remote: dict, monkeypatch) -> None:
    """사용자 전역 설정의 훅이 파이프라인 커밋에서 돌지 않는다."""
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    marker = tmp_path / "hook-ran"
    (hooks / "pre-commit").write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
    (hooks / "pre-commit").chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(f"[core]\n\thooksPath = {hooks}\n")
    monkeypatch.setenv("HOME", str(home))
    data = tmp_path / "data"
    with repo.checkout(data, "o/r", 1, remote["head"], remote["base"], url=remote["url"]) as co:
        (co.path / "app.py").write_text("X = 3\n")
        repo.commit_and_push(co.path, ["app.py"], "m", "pipeline/x", ("me", "me@example.com"))
    assert not marker.exists()


def _redirect_git(worktree: Path, marker: Path) -> None:
    """샌드박스의 PR 코드나 AI가 하는 일: `.git`을 자기 저장소로 바꾸고 fsmonitor를 심는다."""
    evil = worktree / ".evil"
    _git(worktree.parent, "init", "-q", str(evil))
    with (evil / ".git" / "config").open("a") as f:
        f.write(f'[core]\n\tfsmonitor = "touch {marker}; false"\n\tworktree = ../..\n')
    (worktree / ".git").write_text("gitdir: .evil/.git\n")


def test_rewritten_dot_git_is_not_used(tmp_path: Path, remote: dict) -> None:
    """worktree 안의 `.git`을 바꿔도 호스트 git은 만들 때 적어 둔 저장소를 쓴다."""
    data, marker = tmp_path / "data", tmp_path / "PWNED"
    with repo.checkout(data, "o/r", 1, remote["head"], remote["base"], url=remote["url"]) as co:
        _redirect_git(co.path, marker)
        assert "new.py" not in repo.head_diff(co.path)  # 원래 저장소의 HEAD와 비교한다
        repo.status(co.path)
        repo.working_diff(co.path, co.merge_base)
        with repo.side_worktree(co.path, remote["base"]) as side:
            assert (side / "app.py").read_text() == "X = 1\n"
    assert not marker.exists()
    assert not list((data / "worktrees").glob(".*.gitdir"))  # 기록은 worktree와 함께 지운다


def test_worktree_without_record_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "wt"
    path.mkdir()
    _git(path, "init", "-q")
    with pytest.raises(GitError, match="저장소 기록"):
        repo.status(path)


def test_reset_worktree_restores_the_commit_and_removes_build_leftovers(tmp_path: Path) -> None:
    path = tmp_path / "wt"
    path.mkdir()
    _git(path, "init", "-q")
    (path / ".gitignore").write_text("build/\n")
    (path / "app.c").write_text("int main(void) { return 0; }\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "c")
    repo.record_git_dir(path)
    head = _git(path, "rev-parse", "HEAD").strip()
    assert repo.resolve_commit(path, "HEAD") == head
    with pytest.raises(GitError):
        repo.resolve_commit(path, "no-such-branch")
    (path / "app.c").write_text("broken\n")
    (path / "build").mkdir()
    (path / "build" / "CMakeCache.txt").write_text("x\n")
    (path / "stray.o").write_text("x\n")
    repo.reset_worktree(path)
    assert sorted(p.name for p in path.iterdir() if p.name != ".git") == [".gitignore", "app.c"]
    assert (path / "app.c").read_text() == "int main(void) { return 0; }\n"
    repo.forget_git_dir(path)
    with pytest.raises(GitError, match="저장소 기록"):
        repo.status(path)
