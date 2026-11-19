"""PR 코드 준비: bare clone 캐시, worktree, changes.diff (docs/DESIGN.md F4).

git은 호스트에서만 실행한다. worktree의 `.git`은 bare 저장소를 가리키므로 컨테이너 안에서는
동작하지 않는다. 인증은 gh 로그인을 명령 단위로 빌려 쓰고, 전역 git 설정은 바꾸지 않는다.

worktree 안의 `.git` 파일은 믿지 않는다. 샌드박스의 PR 코드(그리고 3단계의 AI)가 worktree에 쓸 수
있어서, `.git`을 자기가 만든 저장소로 바꾸면 그 저장소 설정(`core.fsmonitor`, 필터 등)으로 호스트
git이 임의 명령을 실행한다. 그래서 worktree를 만든 직후, 아무것도 실행하기 전에 저장소 경로를
worktree 밖 파일에 적고(`record_git_dir`), worktree에서 도는 git은 모두 그 경로를 `--git-dir`로
넘긴다(`_tree_git`). 기록이 없으면 실행하지 않는다.
"""

import os
import shutil
import subprocess
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.models.rule_reading import TreeEntry
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


def _git_dir_record(worktree: Path) -> Path:
    """worktree의 저장소 경로를 적는 파일. worktree 밖(옆)에 두어 샌드박스가 바꾸지 못한다."""
    return worktree.parent / f".{worktree.name}.gitdir"


def record_git_dir(worktree: Path) -> None:
    """worktree를 막 만든 뒤(그 안에서 아무것도 실행하기 전)에만 부른다."""
    git_dir = _git("rev-parse", "--absolute-git-dir", cwd=worktree).strip()
    _git_dir_record(worktree).write_text(git_dir)


def _forget_git_dir(worktree: Path) -> None:
    _git_dir_record(worktree).unlink(missing_ok=True)


def _tree_git(worktree: Path, *args: str) -> str:
    """worktree에서 git을 실행한다. worktree 안의 `.git` 대신 만들 때 적어 둔 저장소를 쓴다."""
    try:
        git_dir = _git_dir_record(worktree).read_text().strip()
    except OSError as e:
        raise GitError(f"worktree의 저장소 기록이 없음: {worktree}") from e
    return _git(f"--git-dir={git_dir}", f"--work-tree={worktree}", *args, cwd=worktree)


def worktree_file(worktree: Path, rel: str) -> Path | None:
    """호스트에서 읽고 써도 되는 worktree 안의 일반 파일이면 그 경로, 아니면 None.

    PR(과 샌드박스에서 돈 PR 코드)이 심볼릭 링크를 넣을 수 있다. 호스트에서 링크를 따라가면
    worktree 밖 파일(`~/.ssh` 등)을 읽거나 고친다. 링크 자체는 건너뛰고, 상위 폴더가 링크라
    실제 위치가 worktree 밖이면 거부한다.
    """
    path = worktree / rel
    if path.is_symlink():
        return None
    try:
        real = path.resolve(strict=True)
    except (OSError, RuntimeError):  # 없음, 링크 순환
        return None
    if not real.is_relative_to(worktree.resolve()) or not real.is_file():
        return None
    return path


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
    return _read_base_files(lambda *a: _tree_git(worktree, *a), base_sha, names)


def _read_base_files(
    git: Callable[..., str], base_sha: str, names: tuple[str, ...]
) -> dict[str, str]:
    listed = git("ls-tree", "--name-only", "-z", base_sha, "--", *names)
    present = [name for name in listed.split("\0") if name in names]
    return {name: git("show", f"{base_sha}:{name}") for name in present}


def read_base_files_from_cache(
    data_dir: Path, repo: str, base_sha: str, names: tuple[str, ...], *, url: str | None = None
) -> dict[str, str]:
    """worktree 없이 bare 캐시에서 base 커밋의 파일을 읽는다 (명령 처리용)."""
    bare = ensure_bare(data_dir, repo, url)
    _git("fetch", "--quiet", "--no-tags", "origin", base_sha, cwd=bare)
    return _read_base_files(lambda *a: _git(*a, cwd=bare), base_sha, names)


def list_base_entries(worktree: Path, base_sha: str, paths: Sequence[str]) -> dict[str, TreeEntry]:
    """base 커밋에서 paths 중 있는 것의 (모드, blob 해시, 크기). 경로가 정확히 같은 것만 남긴다.

    내용은 읽지 않는다. 모드로 심볼릭 링크(120000)와 폴더를 가려낸 뒤 `read_blob`으로 읽는다.
    """
    if not paths:  # 경로 없이 부르면 루트의 모든 파일이 나온다
        return {}
    listed = _tree_git(worktree, "ls-tree", "-l", "-z", base_sha, "--", *paths)
    wanted = set(paths)
    entries = {}
    for record in filter(None, listed.split("\0")):
        meta, _, path = record.partition("\t")
        mode, _kind, sha, size = meta.split()
        if path in wanted:
            entries[path] = TreeEntry(path, mode, sha, int(size) if size != "-" else None)
    return entries


def list_base_files(worktree: Path, base_sha: str) -> tuple[str, ...]:
    """base 커밋의 모든 파일 경로 (저장소 기준). 내용은 읽지 않는다."""
    listed = _tree_git(worktree, "ls-tree", "-r", "--name-only", "-z", base_sha)
    return tuple(filter(None, listed.split("\0")))


def read_blob(worktree: Path, sha: str) -> str:
    """blob 하나를 글로 읽는다. 글로 읽을 수 없는 바이트면 UnicodeDecodeError."""
    return _tree_git(worktree, "cat-file", "blob", sha)


def merge_base(worktree: Path, base_sha: str, head_sha: str) -> str:
    return _tree_git(worktree, "merge-base", base_sha, head_sha).strip()


# 사용자 전역 git 설정이 diff 형식(접두사, 경로 표기, 외부 diff 도구)을 바꾸지 않게 한다.
# models.diff가 `+++ b/<경로>` 형식을 읽는다
_DIFF: Final = (
    "-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false", "-c", "diff.relative=false",
    "-c", "core.quotePath=false", "diff", "--no-color", "--no-ext-diff",
)  # fmt: skip


def working_diff(worktree: Path, merge_base_sha: str) -> str:
    """갈라진 커밋과 worktree의 지금 파일 사이의 diff. 수정을 적용한 뒤 바뀐 줄을 다시 셀 때 쓴다.

    base 브랜치의 지금 커밋과 비교하면, PR이 갈라진 뒤 base에서 바뀐 줄까지 PR의 변경으로 잡힌다.
    """
    return _tree_git(worktree, *_DIFF, merge_base_sha)


# 범위 검사용 status. 사용자·리포 git 설정이 결과를 바꾸거나 외부 프로그램을 부르지 않게 한다.
# 전역 무시 파일은 끈다 (리포의 .gitignore만 본다). fsmonitor·untracked 캐시는 결과가 오래될 수
# 있고 fsmonitor는 외부 명령을 부를 수 있다
_STATUS: Final = (
    "-c", "core.quotePath=false", "-c", "core.excludesFile=/dev/null", "-c", "core.fsmonitor=false",
    "-c", "core.untrackedCache=false", "-c", "status.renames=true",
    "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none",
)  # fmt: skip


def status(worktree: Path) -> str:
    """새 파일·삭제·이름 변경까지 보이는 변경 목록 (docs/DESIGN.md F7 범위 검사)."""
    return _tree_git(worktree, *_STATUS)


def ignored_status(worktree: Path) -> str:
    """무시된 파일을 하나씩 보여주는 status (`!!`). F7 무시된 파일 스냅샷용.

    `--ignored=matching`은 무시된 폴더를 `!! build/` 한 줄로만 보여줘, 이미 있던 무시된 폴더 안에
    새로 만든 `build/conftest.py`를 놓친다. `traditional`은 `--untracked-files=all`과 함께 쓰면
    무시된 폴더 안의 파일을 하나씩 낸다.
    """
    return _tree_git(worktree, *_STATUS, "--ignored=traditional")


def outside_symlinks(worktree: Path) -> list[str]:
    """worktree 밖을 가리키는 추적 중인 심볼릭 링크 (리포 기준 경로).

    PR이 `docs -> ~/.ssh` 같은 링크를 넣으면 그 폴더를 거친 쓰기는 worktree 밖으로 가고
    `git status`에도 나오지 않는다. AI를 실행하기 전에 확인한다 (DESIGN.md AI 권한).
    """
    root = worktree.resolve()
    out = []
    for entry in _tree_git(worktree, "ls-files", "-s", "-z").split("\0"):
        meta, _, path = entry.partition("\t")
        if not meta.startswith("120000 "):
            continue
        target = (worktree / path).resolve()
        if not target.is_relative_to(root):
            out.append(path)
    return out


def head_diff(worktree: Path) -> str:
    """체크아웃한 커밋과 worktree 사이의 diff. 수정안 자체다."""
    return _tree_git(worktree, *_DIFF, "HEAD")


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
    _tree_git(worktree, "add", "--", *paths)
    _tree_git(worktree, *_COMMIT_SAFETY, "-c", f"user.name={name}", "-c", f"user.email={email}",
              "commit", "-q", "-m", message)  # fmt: skip
    sha = _tree_git(worktree, "rev-parse", "HEAD").strip()
    _tree_git(worktree, *_COMMIT_SAFETY, "push", "--quiet", "--force", "origin",
              f"HEAD:refs/heads/{branch}")  # fmt: skip
    return sha


def write_changes_diff(worktree: Path, merge_base_sha: str, head_sha: str) -> Path:
    """PR이 바꾼 내용 (`git diff base...head`와 같다). 수정 후 다시 셀 때와 같은 기준을 쓴다."""
    out = worktree / CHANGES_DIFF
    out.parent.mkdir(exist_ok=True)
    out.write_text(_tree_git(worktree, *_DIFF, merge_base_sha, head_sha))
    return out


@contextmanager
def side_worktree(worktree: Path, sha: str) -> Iterator[Path]:
    """같은 저장소에서 다른 커밋의 worktree를 잠깐 만든다 (base 브랜치의 포맷 확인 등)."""
    path = worktree.parent / f"{worktree.name}-{sha[:8]}"
    _tree_git(worktree, "worktree", "add", "--quiet", "--detach", str(path), sha)
    try:
        record_git_dir(path)
        yield path
    finally:
        _forget_git_dir(path)
        try:
            _tree_git(worktree, "worktree", "remove", "--force", str(path))
        except GitError:
            shutil.rmtree(path, ignore_errors=True)
            _tree_git(worktree, "worktree", "prune")


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
        record_git_dir(path)
        fork_point = merge_base(path, base_sha, head_sha)
        write_changes_diff(path, fork_point, head_sha)
        yield Checkout(path=path, head_sha=head_sha, base_sha=base_sha, merge_base=fork_point)
    finally:
        _forget_git_dir(path)
        try:
            _git("worktree", "remove", "--force", str(path), cwd=bare)
        except GitError:
            shutil.rmtree(path, ignore_errors=True)
            _git("worktree", "prune", cwd=bare)
