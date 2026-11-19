"""모델 전용 작업 폴더 (docs/DESIGN.md 8.4, 위협 표 T28).

수정 모델은 PR의 worktree가 아니라 이 폴더에서 돈다. worktree에는 PR이 넣은 심볼릭 링크와,
샌드박스에서 돈 PR 코드가 남긴 링크·특수 파일이 있을 수 있다. 그래서

1. worktree에서 일반 파일만 새 폴더로 복사한다. 링크, 특수 파일, 링크인 폴더 아래의 파일,
   `.git`, 파이프라인 산출물은 복사하지 않는다.
2. 그 폴더를 cwd로 claude를 부르고, PreToolUse 훅(`path_hook.py`)이 도구가 가리키는 경로의
   실제 위치가 폴더 밖이면 거부한다. CLI의 `--restricted` 제한 위에 한 겹을 더한다.
3. 모델이 바꾼 것은 diff로 뽑아 원래 worktree에 `git apply`로 적용한다. git은 링크 너머의 경로에
   쓰지 않는다.

훅이 실제로 돌았는지는 훅의 기록 줄 수로 확인한다. 모델이 도구를 불렀는데 기록이 그보다 적으면
결과를 버린다.
"""

import json
import logging
import os
import shlex
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from app.sandbox.container import OUT_DIR
from app.worker import repo
from app.worker.ai import claude_cli
from app.worker.ai.claude_cli import AIErrorKind, ClaudeResult

# 이보다 큰 파일은 복사하지 않는다. 모델이 볼 일이 없고, 없는 파일은 diff에도 나오지 않는다
MAX_FILE_BYTES: Final = 5 * 1024 * 1024
GIT_TIMEOUT: Final = 120
_HOOK: Final = Path(__file__).with_name("path_hook.py")
# 스냅샷용 git에는 사용자·시스템 설정을 주지 않는다 (필터, 훅, 전역 무시 파일, 줄 끝 변환)
_GIT_ENV: Final = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
_GIT_SAFE: Final = (
    "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "core.autocrlf=false",
    "-c", "core.quotePath=false", "-c", "core.symlinks=true",
)  # fmt: skip

log = logging.getLogger(__name__)


class WorkdirError(RuntimeError):
    """작업 폴더를 만들거나 diff를 뽑는 git이 실패했다."""


@dataclass(frozen=True)
class ModelFolder:
    work: Path  # 모델의 cwd. 일반 파일만 있다
    settings: Path  # 훅을 등록한 설정 파일. work 밖에 있다
    hook_log: Path
    _git_dir: Path
    _base_tree: str

    def hook_calls(self) -> int:
        try:
            return sum(1 for line in self.hook_log.read_text().splitlines() if line.strip())
        except OSError:
            return 0

    def patch(self) -> bytes:
        """모델이 폴더에서 바꾼 것 (새 파일, 삭제 포함). 바뀐 것이 없으면 빈 값."""
        _snapshot_git(self._git_dir, self.work, "add", "-A", "-f", ".")
        return _snapshot_git(
            self._git_dir, self.work, "diff", "--cached", "--binary", "--no-renames",
            "--no-color", "--no-ext-diff", self._base_tree,
        )  # fmt: skip


def _snapshot_git(git_dir: Path, work: Path, *args: str) -> bytes:
    env = {"PATH": os.environ.get("PATH", os.defpath)} | _GIT_ENV
    command = ["git", *_GIT_SAFE, f"--git-dir={git_dir}", f"--work-tree={work}", *args]
    try:
        proc = subprocess.run(
            command, cwd=work, env=env, capture_output=True, timeout=GIT_TIMEOUT, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise WorkdirError(f"git {args[0]} 실행 실패: {e}") from e
    if proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace").strip()[-500:]
        raise WorkdirError(f"git {args[0]} 실패 ({proc.returncode}): {detail}")
    return proc.stdout


def _safe_relative(rel: str) -> bool:
    parts = rel.split("/")
    return bool(rel) and not rel.startswith("/") and all(p not in ("", ".", "..") for p in parts)


def _real_directories(root: Path, rel: str) -> bool:
    """rel의 상위 폴더가 모두 링크가 아닌 진짜 폴더인지."""
    current = root
    for part in rel.split("/")[:-1]:
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except OSError:
            return False
        if not stat.S_ISDIR(mode):
            return False
    return True


def _copy_regular(source: Path, target: Path) -> bool:
    """일반 파일 하나를 복사한다. 링크·특수 파일·너무 큰 파일은 False.

    O_NOFOLLOW로 열고, 연 파일을 fstat으로 다시 확인한다(확인과 열기 사이에 바뀐 경우).
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(source, flags)
    except OSError:
        return False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            return False
        with os.fdopen(fd, "rb", closefd=False) as f:
            data = f.read(MAX_FILE_BYTES + 1)
    except OSError:
        return False
    finally:
        os.close(fd)
    if len(data) > MAX_FILE_BYTES:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    target.chmod(0o755 if info.st_mode & stat.S_IXUSR else 0o644)
    return True


def copy_regular_files(worktree: Path, paths: Sequence[str], work: Path) -> list[str]:
    """paths(worktree 기준) 중 일반 파일만 work로 복사하고, 복사하지 않은 경로를 돌려준다."""
    skipped: list[str] = []
    for rel in paths:
        is_output = rel == OUT_DIR or rel.startswith(f"{OUT_DIR}/")
        if (
            is_output
            or not _safe_relative(rel)
            or rel.split("/")[0] == ".git"
            or not _real_directories(worktree, rel)
            or not _copy_regular(worktree / rel, work / rel)
        ):
            skipped.append(rel)
    return skipped


def hook_settings(work: Path, hook_log: Path, python: str = sys.executable) -> str:
    """경로 감시 훅을 모든 도구에 거는 설정 파일의 내용."""
    command = shlex.join([python, "-I", str(_HOOK), str(work), str(hook_log)])
    hook = {"matcher": "*", "hooks": [{"type": "command", "command": command}]}
    return json.dumps({"hooks": {"PreToolUse": [hook]}})


@contextmanager
def model_folder(worktree: Path, paths: Sequence[str]) -> Iterator[ModelFolder]:
    """worktree의 일반 파일만 든 새 폴더를 만들고, 끝나면 지운다."""
    with tempfile.TemporaryDirectory(prefix="claude-model-") as tmp:
        root = Path(tmp).resolve()  # 훅은 실제 경로로 비교한다 (macOS의 /tmp는 링크다)
        work, git_dir = root / "work", root / "git"
        work.mkdir()
        skipped = copy_regular_files(worktree, paths, work)
        if skipped:
            log.info("모델 폴더에 복사하지 않은 경로 %d개: %s", len(skipped), skipped[:5])
        _snapshot_git(git_dir, work, "init", "-q")
        _snapshot_git(git_dir, work, "add", "-A", "-f", ".")
        tree = _snapshot_git(git_dir, work, "write-tree").decode().strip()
        settings, hook_log = root / "settings.json", root / "hook.log"
        settings.write_text(hook_settings(work, hook_log))
        yield ModelFolder(work, settings, hook_log, git_dir, tree)


def _failed(result: ClaudeResult, kind: AIErrorKind, detail: str) -> ClaudeResult:
    return replace(result, ok=False, error=kind, detail=detail)


def run_in_model_folder(
    worktree: Path,
    prompt: str,
    *,
    run: Callable[..., ClaudeResult] | None = None,
    **kwargs: object,
) -> ClaudeResult:
    """모델 전용 폴더에서 claude를 한 번 실행하고, 바뀐 것을 worktree에 diff로 적용한다.

    훅이 돌지 않았으면(도구 호출 수보다 기록이 적으면) 적용하지 않고 TOOLS 오류로 돌려준다.
    적용에 실패하면 worktree는 그대로이고 APPLY 오류다. run은 테스트용이다.
    """
    run = run or claude_cli.run_claude
    with model_folder(worktree, repo.list_files(worktree)) as folder:
        result = run(folder.work, prompt, settings=folder.settings, **kwargs)
        if result.error == AIErrorKind.TOOLS:
            return result
        hook_calls = folder.hook_calls()
        if hook_calls < result.tool_uses:
            detail = f"경로 감시 훅이 돌지 않음: 도구 호출 {result.tool_uses}건, 훅 {hook_calls}건"
            log.error("%s. 모델이 바꾼 것을 버린다", detail)
            return _failed(result, AIErrorKind.TOOLS, detail)
        patch = folder.patch()
    if not patch:
        return result
    try:
        repo.apply_patch(worktree, patch)
    except repo.GitError as e:
        log.warning("모델의 변경을 worktree에 적용하지 못함: %s", e)
        return _failed(result, AIErrorKind.APPLY, f"변경을 적용하지 못함: {e}")
    return result
