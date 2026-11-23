"""모델 전용 작업 폴더 (docs/DESIGN.md 8.4, 위협 표 T28).

수정 모델은 PR의 worktree가 아니라 이 폴더에서 돈다. worktree에는 PR이 넣은 심볼릭 링크와,
샌드박스에서 돈 PR 코드가 남긴 링크·특수 파일이 있을 수 있다. 그래서

1. worktree에서 일반 파일만 새 폴더로 복사한다. 링크, 특수 파일, 링크인 폴더 아래의 파일,
   `.git`, 파이프라인 산출물은 복사하지 않는다.
2. 그 폴더를 cwd로 claude를 부르고, PreToolUse 훅(`path_hook.py`)이 도구가 가리키는 경로의
   실제 위치가 폴더 밖이면 거부한다. CLI의 `--restricted` 제한 위에 한 겹을 더한다.
3. 모델이 바꾼 것은 diff로 뽑아 원래 worktree에 `git apply`로 적용한다. git은 링크 너머의 경로에
   쓰지 않는다.
4. 파이프라인 도구(규칙 검사, 테스트)를 주면 도구를 돌리기 직전에 그때까지 바뀐 것을 worktree에
   적용한다(`ModelFolder.sync`). 도구는 worktree에서 돌고, 호출이 끝나면 남은 것만 적용한다.

훅이 실제로 돌았는지는 훅의 기록 줄 수로 확인한다. 모델이 도구를 불렀는데 기록이 그보다 적으면
결과를 버린다.

위반 판단(4.5)도 같은 폴더와 훅에서 읽기 도구(Read, Grep, Glob)로 PR의 코드를 읽는다
(`reading_folder`). 쓰기 도구가 없고, 폴더의 변경은 worktree에 적용하지 않는다.

폴더 만들기·복사·변경 적용(`model_folder`, `ModelFolder`, `path_digest`)은 어느 모델 구현이든 같이
쓰는 것이고, 경로 감시 훅(`hook_settings`, 훅 기록 수 확인)과 그것을 claude에 넘기는
`reading_folder`, `run_in_model_folder`는 claude 구현(`claude_backend.ClaudeCliBackend`)의 것이다.
"""

import hashlib
import json
import logging
import os
import shlex
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from app.worker import repo
from app.worker.ai import claude_cli, tool_server
from app.worker.ai.model_backend import AIErrorKind, AskModel, EditResult, StructuredResult
from app.worker.ai.tool_server import Handler
from app.worker.sandbox.container import OUT_DIR

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


@dataclass
class ModelFolder:
    work: Path  # 모델의 cwd. 일반 파일만 있다
    settings: Path  # 훅을 등록한 설정 파일. work 밖에 있다
    hook_log: Path
    _git_dir: Path
    _base_tree: str
    _synced_tree: str = ""  # worktree에 이미 적용한 폴더 상태. 비면 처음 상태(_base_tree)

    def _tree(self) -> str:
        _snapshot_git(self._git_dir, self.work, "add", "-A", "-f", ".")
        return _snapshot_git(self._git_dir, self.work, "write-tree").decode().strip()

    def _diff(self, old: str, new: str) -> bytes:
        return _snapshot_git(
            self._git_dir, self.work, "diff", "--binary", "--no-renames", "--no-color",
            "--no-ext-diff", old, new,
        )  # fmt: skip

    def sync(self, worktree: Path) -> None:
        """지금까지 폴더에서 바뀐 것 중 아직 worktree에 없는 것을 적용한다. 실패하면 GitError."""
        synced, now = self._synced_tree or self._base_tree, self._tree()
        if now != synced:
            repo.apply_patch(worktree, self._diff(synced, now))
        self._synced_tree = now

    def rest(self) -> bytes:
        """아직 worktree에 적용하지 않은 변경."""
        return self._diff(self._synced_tree or self._base_tree, self._tree())

    def unsync(self, worktree: Path) -> None:
        """sync로 적용한 것을 worktree에서 되돌린다. 결과를 버릴 때 쓴다."""
        synced = self._synced_tree or self._base_tree
        if synced != self._base_tree:
            repo.apply_patch(worktree, self._diff(self._base_tree, synced), reverse=True)
        self._synced_tree = self._base_tree

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


def _read_regular(source: Path) -> tuple[bytes, int] | None:
    """일반 파일 하나의 (내용, 모드). 링크·특수 파일·너무 큰 파일은 None.

    O_NOFOLLOW로 열고, 연 파일을 fstat으로 다시 확인한다(확인과 열기 사이에 바뀐 경우).
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(source, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            return None
        with os.fdopen(fd, "rb", closefd=False) as f:
            data = f.read(MAX_FILE_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    return None if len(data) > MAX_FILE_BYTES else (data, info.st_mode)


def _copyable(worktree: Path, rel: str) -> tuple[bytes, int] | None:
    """모델 폴더에 복사될 파일이면 (내용, 모드), 아니면 None. 안전한 상대 경로, `.git`과 파이프라인
    산출물이 아님, 상위 폴더가 모두 진짜 폴더, 일반 파일, 크기 상한 안."""
    is_output = rel == OUT_DIR or rel.startswith(f"{OUT_DIR}/")
    if (
        is_output
        or not _safe_relative(rel)
        or rel.split("/")[0] == ".git"
        or not _real_directories(worktree, rel)
    ):
        return None
    return _read_regular(worktree / rel)


def regular_bytes(worktree: Path, rel: str) -> bytes | None:
    """모델 폴더에 복사될 파일이면 그 내용, 아니면 None (복사와 같은 조건)."""
    found = _copyable(worktree, rel)
    return None if found is None else found[0]


def copy_regular_files(worktree: Path, paths: Sequence[str], work: Path) -> list[str]:
    """paths(worktree 기준) 중 일반 파일만 work로 복사하고, 복사하지 않은 경로를 돌려준다."""
    skipped: list[str] = []
    for rel in paths:
        found = _copyable(worktree, rel)
        if found is None:
            skipped.append(rel)
            continue
        data, mode = found
        target = work / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o755 if mode & stat.S_IXUSR else 0o644)
    return skipped


def path_digest(worktree: Path, rel: str, files: Sequence[str]) -> str:
    """모델 폴더에서 rel이 가리키던 것의 해시. files는 `repo.list_files(worktree)`다.

    파일이면 그 내용, 폴더(또는 ".")면 그 아래 복사될 파일들의 경로와 내용, 아무것도 없으면 "-".
    위반 판단이 저장한 답을 다시 쓸 때 모델이 읽은 곳이 그대로인지 맞춰 보는 데 쓴다 (4.5)."""
    if rel in files:
        data = regular_bytes(worktree, rel)
        return "-" if data is None else hashlib.sha256(data).hexdigest()
    prefix = "" if rel == "." else rel.rstrip("/") + "/"
    entries = []
    for path in files:
        if path.startswith(prefix):
            data = regular_bytes(worktree, path)
            if data is not None:
                entries.append([path, hashlib.sha256(data).hexdigest()])
    if not entries:
        return "-"
    return hashlib.sha256(json.dumps(entries, ensure_ascii=False).encode()).hexdigest()


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


def _failed(result: EditResult, kind: AIErrorKind, detail: str) -> EditResult:
    return replace(result, ok=False, error=kind, detail=detail)


@contextmanager
def reading_folder(worktree: Path, ask: AskModel) -> Iterator[AskModel]:
    """worktree의 일반 파일만 복사한 모델 폴더를 열고, 그 폴더에서 읽기 도구로 묻는 함수를 준다.
    ask는 `claude_cli.run_structured`의 모양이다(`read_in`을 받는다). claude 구현의 `reading`이다.

    폴더 하나를 블록 안의 호출들이 같이 쓰고 블록이 끝나면 지운다. 모델에게는 쓰기 도구가 없고,
    폴더에서 무엇이 바뀌어도 worktree에 적용하지 않는다. 호출마다 경로 감시 훅의 기록이 모델이 부른
    도구 수만큼 늘었는지 보고, 모자라면 답을 버리고 TOOLS 오류로 돌려준다(수정 작성과 같은 확인).
    """
    with model_folder(worktree, repo.list_files(worktree)) as folder:
        where = claude_cli.ReadFolder(folder.work, folder.settings)

        def ask_in_folder(prompt: str, **kwargs: object) -> StructuredResult:
            before = folder.hook_calls()
            result = ask(prompt, read_in=where, **kwargs)
            ran = folder.hook_calls() - before
            if result.error != AIErrorKind.TOOLS and ran < result.tool_uses:
                detail = f"경로 감시 훅이 돌지 않음: 도구 호출 {result.tool_uses}건, 훅 {ran}건"
                log.error("%s. 위반 판단의 답을 버린다", detail)
                return replace(result, ok=False, error=AIErrorKind.TOOLS, data=None, detail=detail)
            return result

        yield ask_in_folder


def _synced_first(folder: ModelFolder, worktree: Path, handler: Handler) -> Handler:
    def run(args: Mapping[str, str]) -> str:
        folder.sync(worktree)
        return handler(args)

    return run


def run_in_model_folder(
    worktree: Path,
    prompt: str,
    *,
    run: Callable[..., EditResult] | None = None,
    tools: Mapping[str, Handler] | None = None,
    **kwargs: object,
) -> EditResult:
    """모델 전용 폴더에서 claude를 한 번 실행하고, 바뀐 것을 worktree에 diff로 적용한다.
    claude 구현의 `fix`다.

    tools(이름 → 파이프라인 도구)가 있으면 도구 서버를 열어 `fix_tools` 모양으로 부른다. 도구는
    돌기 직전에 폴더의 지금 상태를 worktree에 맞춘다. 도구 호출 수와 시간은 결과의 `tool_usage`다.
    훅이 돌지 않았으면(도구 호출 수보다 기록이 적으면) 적용한 것까지 되돌리고 TOOLS 오류로 돌려준다.
    적용에 실패하면 APPLY 오류다. 적용한 diff 전체는 결과의 `applied`에 담는다(수정 작성이 저장해
    같은 입력에 다시 쓴다). run은 테스트용이다.
    """
    run = run or claude_cli.run_claude
    with model_folder(worktree, repo.list_files(worktree)) as folder:
        if tools:
            wrapped = {name: _synced_first(folder, worktree, h) for name, h in tools.items()}
            with tool_server.serving(wrapped) as server:
                result = run(folder.work, prompt, settings=folder.settings,
                             mcp_config=server.config, **kwargs)  # fmt: skip
                result = replace(result, tool_usage=server.usage())
        else:
            result = run(folder.work, prompt, settings=folder.settings, **kwargs)
        hook_calls = folder.hook_calls()
        if result.error != AIErrorKind.TOOLS and hook_calls < result.tool_uses:
            detail = f"경로 감시 훅이 돌지 않음: 도구 호출 {result.tool_uses}건, 훅 {hook_calls}건"
            log.error("%s. 모델이 바꾼 것을 버린다", detail)
            result = _failed(result, AIErrorKind.TOOLS, detail)
        if result.error == AIErrorKind.TOOLS:
            folder.unsync(worktree)
            return result
        patch, rest = folder.patch(), folder.rest()
    try:
        if rest:
            repo.apply_patch(worktree, rest)
    except repo.GitError as e:
        log.warning("모델의 변경을 worktree에 적용하지 못함: %s", e)
        return _failed(result, AIErrorKind.APPLY, f"변경을 적용하지 못함: {e}")
    return replace(result, applied=patch) if patch else result
