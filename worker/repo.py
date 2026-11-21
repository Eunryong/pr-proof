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
import re
import shutil
import subprocess
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from worker.rule_reading_pure import TreeEntry
from worker.rules.pr_check_pure import RawCommit
from worker.sandbox.container import OUT_DIR

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


def fetch_commit(bare: Path, sha: str) -> None:
    """커밋 하나를 bare 캐시로 가져온다. 이미 있으면 받지 않는다. PR head는 가져오지 않는다."""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise GitError(f"커밋 해시가 아님: {sha!r}")
    try:
        _git("cat-file", "-e", f"{sha}^{{commit}}", cwd=bare)
        return
    except GitError:
        pass
    _git("fetch", "--quiet", "--no-tags", "origin", sha, cwd=bare)


def bare_entries(bare: Path, sha: str, paths: Sequence[str]) -> dict[str, TreeEntry]:
    """worktree 없이 bare 캐시의 커밋에서 paths의 (모드, blob 해시, 크기)를 읽는다."""
    if not paths:
        return {}
    listed = _git("ls-tree", "-l", "-z", sha, "--", *paths, cwd=bare)
    wanted = set(paths)
    return {path: e for path, e in _tree_entries(listed).items() if path in wanted}


def bare_blob(bare: Path, blob_sha: str) -> str:
    """bare 캐시의 blob 하나를 글로 읽는다. 글로 읽을 수 없는 바이트면 UnicodeDecodeError."""
    return _git("cat-file", "blob", blob_sha, cwd=bare)


def list_base_entries(worktree: Path, base_sha: str, paths: Sequence[str]) -> dict[str, TreeEntry]:
    """base 커밋에서 paths 중 있는 것의 (모드, blob 해시, 크기). 경로가 정확히 같은 것만 남긴다.

    내용은 읽지 않는다. 모드로 심볼릭 링크(120000)와 폴더를 가려낸 뒤 `read_blob`으로 읽는다.
    """
    if not paths:  # 경로 없이 부르면 루트의 모든 파일이 나온다
        return {}
    listed = _tree_git(worktree, "ls-tree", "-l", "-z", base_sha, "--", *paths)
    wanted = set(paths)
    return {path: e for path, e in _tree_entries(listed).items() if path in wanted}


def base_root_entries(worktree: Path, base_sha: str) -> dict[str, TreeEntry]:
    """base 커밋 루트의 모든 항목 (모드, blob 해시, 크기). 설정 파일을 고를 때 쓴다."""
    return _tree_entries(_tree_git(worktree, "ls-tree", "-l", "-z", base_sha))


def _tree_entries(listed: str) -> dict[str, TreeEntry]:
    entries = {}
    for record in filter(None, listed.split("\0")):
        meta, _, path = record.partition("\t")
        mode, _kind, sha, size = meta.split()
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


def changed_paths(worktree: Path, merge_base_sha: str, head_sha: str) -> tuple[str, ...]:
    """PR이 바꾼 파일의 경로 (4.5 PR 사실). 추가·수정·삭제이고, 이름을 바꾸면 옛 경로와 새 경로
    둘 다다."""
    listed = _tree_git(
        worktree, "-c", "core.quotePath=false", "diff", "--name-only", "-z", "--no-renames",
        merge_base_sha, head_sha,
    )  # fmt: skip
    return tuple(filter(None, listed.split("\0")))


# 커밋 객체에서 메시지 앞의 머리(작성자, 서명 등)에 둘 여유. 그 뒤는 읽지 않고 버린다
_COMMIT_HEADER_ROOM: Final = 64 * 1024
_FULL_SHA_LINE: Final = re.compile(r"[0-9a-f]{40}( [0-9a-f]{40})*")


def pr_commits(
    worktree: Path, merge_base_sha: str, head_sha: str, *, limit: int, message_bytes: int
) -> tuple[list[RawCommit], bool]:
    """PR의 커밋(merge-base..head, 새 커밋부터) limit개까지와, 더 있었는지 (4.5 PR 사실).

    해시와 부모는 `rev-list --parents`로, 메시지는 `cat-file --batch`로 객체 크기를 보며 읽는다.
    메시지 안의 글(작성자의 글)이 구분자를 흉내 내어 다른 커밋처럼 보일 수 없다. 메시지는
    message_bytes를 조금 넘게까지만 읽고, 객체가 더 크면 cut으로 표시한다.
    """
    listed = _tree_git(
        worktree, "rev-list", "--parents", f"--max-count={limit + 1}",
        f"{merge_base_sha}..{head_sha}",
    )  # fmt: skip
    rows = [line.split() for line in listed.splitlines() if line.strip()]
    if not all(_FULL_SHA_LINE.fullmatch(" ".join(r)) for r in rows):
        raise GitError("rev-list 출력을 읽지 못함")
    more = len(rows) > limit
    rows = rows[:limit]
    messages = _commit_messages(worktree, [r[0] for r in rows], message_bytes + _COMMIT_HEADER_ROOM)
    commits = [RawCommit(r[0], tuple(r[1:]), *messages[r[0]]) for r in rows]
    return commits, more


def _commit_messages(worktree: Path, shas: Sequence[str], keep: int) -> dict[str, tuple[str, bool]]:
    """커밋마다 (메시지, 잘렸는지). 객체의 앞 keep바이트만 메모리에 둔다."""
    if not shas:
        return {}
    try:
        git_dir = _git_dir_record(worktree).read_text().strip()
    except OSError as e:
        raise GitError(f"worktree의 저장소 기록이 없음: {worktree}") from e
    env = {k: os.environ[k] for k in _ENV_PASSTHROUGH if k in os.environ}
    command = ["git", f"--git-dir={git_dir}", "cat-file", "--batch"]
    try:
        proc = subprocess.Popen(
            command, cwd=worktree, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env,
        )  # fmt: skip
    except OSError as e:
        raise GitError(f"git cat-file 실행 실패: {e}") from e
    out: dict[str, tuple[str, bool]] = {}
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(("\n".join(shas) + "\n").encode())
        proc.stdin.close()
        for sha in shas:
            header = proc.stdout.readline().split()
            if len(header) != 3 or header[0].decode() != sha or header[1] != b"commit":
                raise GitError(f"커밋 객체를 읽지 못함: {sha}")
            size = int(header[2])
            head = proc.stdout.read(min(size, keep))
            left = size - len(head)
            while left > 0:
                chunk = proc.stdout.read(min(left, 1 << 16))
                if not chunk:
                    raise GitError(f"커밋 객체가 중간에 끝남: {sha}")
                left -= len(chunk)
            proc.stdout.read(1)  # 객체 뒤의 줄바꿈
            _, _, message = head.partition(b"\n\n")
            out[sha] = (message.decode("utf-8", errors="replace"), size > keep)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=GIT_TIMEOUT)
    return out


# 사용자 전역 git 설정이 diff 형식(접두사, 경로 표기, 외부 diff 도구)을 바꾸지 않게 한다.
# diff_pure가 `+++ b/<경로>` 형식을 읽는다
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


def list_files(worktree: Path) -> list[str]:
    """추적 중인 파일과 무시되지 않은 새 파일 (리포 기준 경로). 모델 폴더로 복사할 후보다."""
    out = _tree_git(
        worktree, "-c", "core.quotePath=false", "-c", "core.excludesFile=/dev/null",
        "-c", "core.fsmonitor=false", "ls-files", "-z", "--cached", "--others",
        "--exclude-standard",
    )  # fmt: skip
    return sorted({path for path in out.split("\0") if path})


def apply_patch(worktree: Path, patch: bytes, *, reverse: bool = False) -> None:
    """모델 폴더에서 뽑은 diff를 worktree의 파일에 적용한다. 인덱스는 건드리지 않는다.

    git은 심볼릭 링크 너머의 경로에 쓰지 않고("beyond a symbolic link"), 하나라도 맞지 않으면
    아무것도 적용하지 않는다. reverse면 diff를 거꾸로 적용해 되돌린다.
    """
    try:
        git_dir = _git_dir_record(worktree).read_text().strip()
    except OSError as e:
        raise GitError(f"worktree의 저장소 기록이 없음: {worktree}") from e
    env = {k: os.environ[k] for k in _ENV_PASSTHROUGH if k in os.environ}
    command = [
        "git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
        "-c", "core.autocrlf=false", f"--git-dir={git_dir}", f"--work-tree={worktree}",
        "apply", "--binary", "--whitespace=nowarn", *(["-R"] if reverse else []), "-",
    ]  # fmt: skip
    try:
        proc = subprocess.run(
            command, cwd=worktree, input=patch, capture_output=True, env=env,
            timeout=GIT_TIMEOUT, check=False,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GitError(f"git apply 실행 실패: {e}") from e
    if proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace").strip()[-500:]
        raise GitError(f"git apply 실패 ({proc.returncode}): {detail}")


def commit_diff(worktree: Path, old: str, new: str, path: str) -> str:
    """두 커밋 사이의 파일 하나의 diff. 작성자가 자기 커밋으로 고친 기록(4.10)에 쓴다."""
    return _tree_git(worktree, *_DIFF, old, new, "--", path)


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
