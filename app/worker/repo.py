"""PR 코드 준비: bare clone 캐시, worktree, changes.diff (docs/DESIGN.md F4).

git은 호스트에서만 실행한다. worktree의 `.git`은 bare 저장소를 가리키므로 컨테이너 안에서는
동작하지 않는다. 인증은 gh 로그인을 명령 단위로 빌려 쓰고, 전역 git 설정은 바꾸지 않는다.
"""

import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.sandbox.container import OUT_DIR

CHANGES_DIFF: Final = f"{OUT_DIR}/changes.diff"
GIT_TIMEOUT: Final = 300
_ENV_PASSTHROUGH: Final = ("PATH", "HOME", "GH_CONFIG_DIR")
# 이 명령에서만 gh를 자격 증명 도우미로 쓴다. 앞의 빈 값은 사용자의 다른 도우미를 끈다
_GH_CREDENTIAL: Final = (
    "-c",
    "credential.helper=",
    "-c",
    "credential.helper=!gh auth git-credential",
)


class GitError(RuntimeError):
    """git 명령 실패. 네트워크·인증 문제일 수 있어 환경 오류로 다룬다."""


@dataclass(frozen=True)
class Checkout:
    path: Path
    head_sha: str
    base_sha: str  # base 브랜치의 지금 커밋. 설정·규칙은 여기서 읽는다
    # PR 브랜치가 갈라진 커밋. PR이 바꾼 줄은 여기와 비교한다. base가 앞서 나가면 base_sha와 다르다
    merge_base: str


def _git(*args: str, cwd: Path | None = None) -> str:
    env = {k: os.environ[k] for k in _ENV_PASSTHROUGH if k in os.environ}
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.run(
            ["git", *_GH_CREDENTIAL, *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            env=env,
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GitError(f"git {args[0]} 실행 실패: {e}") from e
    if proc.returncode != 0:
        raise GitError(f"git {args[0]} 실패 ({proc.returncode}): {proc.stderr.strip()[-1000:]}")
    return proc.stdout


def remote_url(repo: str) -> str:
    return f"https://github.com/{repo}.git"


def bare_path(data_dir: Path, repo: str) -> Path:
    return data_dir / "repos" / f"{repo.replace('/', '__')}.git"


def ensure_bare(data_dir: Path, repo: str, url: str | None = None) -> Path:
    """리포마다 처음 한 번만 bare clone한다."""
    path = bare_path(data_dir, repo)
    if not (path / "HEAD").is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        _git("clone", "--bare", "--quiet", url or remote_url(repo), str(path))
    return path


def fetch_pr(bare: Path, pr_number: int, base_sha: str) -> None:
    """PR head와 base 커밋을 가져온다. base는 SHA로 가져온다 (GitHub은 도달 가능한 SHA를 허용)."""
    _git(
        "fetch", "--quiet", "--no-tags", "origin",
        f"+refs/pull/{pr_number}/head:refs/pipeline/pr/{pr_number}",
        base_sha,
        cwd=bare,
    )  # fmt: skip


def read_base_files(worktree: Path, base_sha: str, names: tuple[str, ...]) -> dict[str, str]:
    """base 커밋의 리포 최상위 파일 중 있는 것만 {이름: 내용}으로 읽는다.

    설정과 규칙은 PR head가 아니라 base에서 읽는다 (PR이 자기 검사 기준을 바꾸지 못하게).
    """
    listed = _git("ls-tree", "--name-only", "-z", base_sha, "--", *names, cwd=worktree)
    present = [name for name in listed.split("\0") if name in names]
    return {name: _git("show", f"{base_sha}:{name}", cwd=worktree) for name in present}


def read_base_files_from_cache(
    data_dir: Path, repo: str, base_sha: str, names: tuple[str, ...], *, url: str | None = None
) -> dict[str, str]:
    """worktree 없이 bare 캐시에서 base 커밋의 파일을 읽는다 (명령 처리용)."""
    bare = ensure_bare(data_dir, repo, url)
    _git("fetch", "--quiet", "--no-tags", "origin", base_sha, cwd=bare)
    return read_base_files(bare, base_sha, names)


def merge_base(worktree: Path, base_sha: str, head_sha: str) -> str:
    return _git("merge-base", base_sha, head_sha, cwd=worktree).strip()


def working_diff(worktree: Path, merge_base_sha: str) -> str:
    """갈라진 커밋과 worktree의 지금 파일 사이의 diff. 수정을 적용한 뒤 바뀐 줄을 다시 셀 때 쓴다.

    base 브랜치의 지금 커밋과 비교하면, PR이 갈라진 뒤 base에서 바뀐 줄까지 PR의 변경으로 잡힌다.
    """
    return _git("diff", "--no-color", merge_base_sha, cwd=worktree)


def status(worktree: Path) -> str:
    """새 파일·삭제·이름 변경까지 보이는 변경 목록 (docs/DESIGN.md F7 범위 검사)."""
    return _git("status", "--porcelain=v1", "-z", "--untracked-files=all", cwd=worktree)


def head_diff(worktree: Path) -> str:
    """체크아웃한 커밋과 worktree 사이의 diff. 수정안 자체다."""
    return _git("diff", "--no-color", "HEAD", cwd=worktree)


# 사용자 전역 git 설정의 서명·훅이 파이프라인 커밋에 끼어들지 않게 한다
_COMMIT_SAFETY: Final = ("-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null")


def commit_and_push(
    worktree: Path,
    paths: Sequence[str],
    message: str,
    branch: str,
    author: tuple[str, str],
) -> str:
    """허용된 경로만 스테이징해 커밋하고 파이프라인 브랜치로 강제 푸시한다. 커밋 SHA를 돌려준다.

    `git add -A`를 쓰지 않는다. `.pipeline-out/`이나 테스트 산출물이 섞이지 않게 한다 (F10).
    """
    name, email = author
    _git("add", "--", *paths, cwd=worktree)
    _git(*_COMMIT_SAFETY, "-c", f"user.name={name}", "-c", f"user.email={email}",
         "commit", "-q", "-m", message, cwd=worktree)  # fmt: skip
    sha = _git("rev-parse", "HEAD", cwd=worktree).strip()
    _git(*_COMMIT_SAFETY, "push", "--quiet", "--force", "origin", f"HEAD:refs/heads/{branch}",
         cwd=worktree)  # fmt: skip
    return sha


def write_changes_diff(worktree: Path, merge_base_sha: str, head_sha: str) -> Path:
    """PR이 바꾼 내용 (`git diff base...head`와 같다). 수정 후 다시 셀 때와 같은 기준을 쓴다."""
    out = worktree / CHANGES_DIFF
    out.parent.mkdir(exist_ok=True)
    out.write_text(_git("diff", "--no-color", merge_base_sha, head_sha, cwd=worktree))
    return out


@contextmanager
def side_worktree(worktree: Path, sha: str) -> Iterator[Path]:
    """같은 저장소에서 다른 커밋의 worktree를 잠깐 만든다 (base 브랜치의 포맷 확인 등)."""
    path = worktree.parent / f"{worktree.name}-{sha[:8]}"
    _git("worktree", "add", "--quiet", "--detach", str(path), sha, cwd=worktree)
    try:
        yield path
    finally:
        try:
            _git("worktree", "remove", "--force", str(path), cwd=worktree)
        except GitError:
            shutil.rmtree(path, ignore_errors=True)
            _git("worktree", "prune", cwd=worktree)


@contextmanager
def checkout(
    data_dir: Path,
    repo: str,
    pr_number: int,
    head_sha: str,
    base_sha: str,
    *,
    url: str | None = None,
) -> Iterator[Checkout]:
    """PR head로 worktree를 만들고 changes.diff를 남긴다. 블록을 나가면 worktree를 지운다."""
    bare = ensure_bare(data_dir, repo, url)
    fetch_pr(bare, pr_number, base_sha)
    path = (
        data_dir / "worktrees" / f"{repo.replace('/', '__')}-pr{pr_number}-{uuid.uuid4().hex[:8]}"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    _git("worktree", "add", "--quiet", "--detach", str(path), head_sha, cwd=bare)
    try:
        fork_point = merge_base(path, base_sha, head_sha)
        write_changes_diff(path, fork_point, head_sha)
        yield Checkout(path=path, head_sha=head_sha, base_sha=base_sha, merge_base=fork_point)
    finally:
        try:
            _git("worktree", "remove", "--force", str(path), cwd=bare)
        except GitError:
            shutil.rmtree(path, ignore_errors=True)
            _git("worktree", "prune", cwd=bare)
