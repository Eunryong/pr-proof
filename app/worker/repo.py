"""PR 코드 준비: bare clone 캐시, worktree, changes.diff (docs/DESIGN.md F4).

git은 호스트에서만 실행한다. worktree의 `.git`은 bare 저장소를 가리키므로 컨테이너 안에서는
동작하지 않는다. 인증은 gh 로그인을 명령 단위로 빌려 쓰고, 전역 git 설정은 바꾸지 않는다.
"""

import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator
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
    base_sha: str


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


def write_changes_diff(worktree: Path, base_sha: str, head_sha: str) -> Path:
    out = worktree / CHANGES_DIFF
    out.parent.mkdir(exist_ok=True)
    out.write_text(_git("diff", "--no-color", f"{base_sha}...{head_sha}", cwd=worktree))
    return out


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
        write_changes_diff(path, base_sha, head_sha)
        yield Checkout(path=path, head_sha=head_sha, base_sha=base_sha)
    finally:
        try:
            _git("worktree", "remove", "--force", str(path), cwd=bare)
        except GitError:
            shutil.rmtree(path, ignore_errors=True)
            _git("worktree", "prune", cwd=bare)
