"""검사기를 샌드박스에서 돌리고 결과를 읽는다 (docs/DESIGN.md 4.4 예시 확인, 4.5, 8.1).

검사기 하나를 컨테이너 하나에서 돌린다. 네트워크 없음, 루트 파일 시스템 읽기 전용, 권한 없음,
자원 제한, 호스트 UID. 검사할 폴더(worktree 또는 예시 폴더), 검사기 파일, 도구 캐시는 모두 읽기
전용이다. 작업 디렉터리는 검사기 폴더라서 검사할 폴더의 `sgconfig.yml`을 읽지 않는다 (PR이 넣은
설정이 언어 연결을 바꾸면 위반이 숨는다).

출력은 믿지 않는 입력이다. 크기 상한 안의 JSON만 읽고, 넘긴 파일 밖을 가리키는 항목이 있으면
결과 전체를 버린다.
"""

import json
import subprocess
import tempfile
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

from app.models.checker import CheckerKind, validate_body
from app.sandbox import docker_cli

WORK: Final = "/work"
CHECKER_DIR: Final = "/checker"
TOOL_MOUNT: Final = "/tools/ast-grep"
RULE_FILE: Final = "rule.yml"
SCRIPT_FILE: Final = "check.py"
EXAMPLE_DIR: Final = "examples"

CHECK_TIMEOUT: Final = 120
MAX_OUTPUT_BYTES: Final = 8 * 1024 * 1024
MAX_RESULTS: Final = 5000
MAX_STDERR_CHARS: Final = 1500
POLL_SECONDS: Final = 0.2
# ast-grep: 0 위반 없음, 1 위반 있음(--error로 모든 규칙을 error로 올린다). 그 밖은 규칙 오류
_AST_GREP_OK: Final = frozenset({0, 1})
_KILLED: Final = 137


@dataclass(frozen=True)
class Match:
    file: str  # 넘긴 파일 중 하나 (상대 경로)
    line: int  # 1부터
    end_line: int
    message: str  # ast-grep 규칙의 message 또는 스크립트의 why


@dataclass(frozen=True)
class Ran:
    """검사기가 끝까지 돌았다. 위반이 없으면 matches가 비어 있다."""

    matches: tuple[Match, ...]


@dataclass(frozen=True)
class Invalid:
    """검사기 자체가 틀렸다 (실행 전 확인에서 거절, 엔진이 규칙을 읽지 못함, 스크립트 오류)."""

    reason: str


@dataclass(frozen=True)
class Failed:
    """검사기는 돌았지만 결과를 쓸 수 없다 (시간 초과, 메모리, 출력 크기·형식)."""

    reason: str


CheckerRun = Ran | Invalid | Failed


def run_checker(
    kind: CheckerKind,
    body: str,
    root: Path,
    files: Sequence[str],
    *,
    tool_dir: Path | None = None,
    timeout: int = CHECK_TIMEOUT,
    memory_gb: float = docker_cli.MEMORY_LIMIT_GB,
) -> CheckerRun:
    """root 아래 files(상대 경로)에 검사기를 돌린다. ast-grep은 tool_dir(도구 캐시)이 필요하다.

    docker 자체가 실패하면 SandboxError를 올린다.
    """
    _check_paths(files)
    problem = validate_body(kind, body)
    if problem:
        return Invalid(problem)
    if not files:
        return Ran(())
    if kind == "ast-grep" and tool_dir is None:
        raise ValueError("ast-grep 검사기에는 tool_dir이 필요하다")
    with tempfile.TemporaryDirectory(prefix="pipeline-checker-") as tmp:
        checker_dir = Path(tmp)
        if kind == "ast-grep":
            (checker_dir / RULE_FILE).write_text(body)
            mounts = ("-v", f"{tool_dir.resolve()}:{TOOL_MOUNT}:ro")
            argv = [
                f"{TOOL_MOUNT}/ast-grep", "scan", "--rule", f"{CHECKER_DIR}/{RULE_FILE}",
                "--json=compact", "--color", "never", "--error", "--max-results",
                str(MAX_RESULTS), *(f"{WORK}/{f}" for f in files),
            ]  # fmt: skip
            stdin = b""
        else:
            (checker_dir / SCRIPT_FILE).write_text(body)
            mounts = ()
            argv = ["python", "-I", f"{CHECKER_DIR}/{SCRIPT_FILE}"]
            stdin = json.dumps({"files": list(files)}).encode()
        workdir = CHECKER_DIR if kind == "ast-grep" else WORK
        docker = [
            "run", "--rm", "-i", "--network", "none", "--read-only", *docker_cli.limits(memory_gb),
            "--user", docker_cli.user(), "-e", "HOME=/tmp", "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-v", f"{root.resolve()}:{WORK}:ro", "-v", f"{checker_dir}:{CHECKER_DIR}:ro", *mounts,
            "-w", workdir, docker_cli.TOOL_IMAGE, *argv,
        ]  # fmt: skip
        result = _run_bounded(docker, stdin, timeout=timeout)
    return read_result(kind, result, frozenset(files), timeout)


def run_on_examples(
    kind: CheckerKind,
    body: str,
    snippets: Sequence[str],
    *,
    tool_dir: Path | None = None,
    timeout: int = CHECK_TIMEOUT,
) -> CheckerRun:
    """예시 코드마다 파일을 하나씩 만들어 검사기를 돌린다. 저장소는 마운트하지 않는다.

    Match.file은 `example_path(i)`이다.
    """
    with tempfile.TemporaryDirectory(prefix="pipeline-examples-") as tmp:
        root = Path(tmp)
        (root / EXAMPLE_DIR).mkdir()
        for i, code in enumerate(snippets):
            (root / example_path(i)).write_text(code)
        files = [example_path(i) for i in range(len(snippets))]
        return run_checker(kind, body, root, files, tool_dir=tool_dir, timeout=timeout)


def example_path(index: int) -> str:
    return f"{EXAMPLE_DIR}/example_{index}.py"


def _check_paths(files: Sequence[str]) -> None:
    for f in files:
        if not _is_relative_inside(f):
            raise ValueError(f"검사할 파일은 root 안의 상대 경로여야 한다: {f!r}")


def _is_relative_inside(path: str) -> bool:
    p = PurePosixPath(path)
    return bool(path) and not p.is_absolute() and ".." not in p.parts and "\0" not in path


@dataclass(frozen=True)
class RawRun:
    """docker run 한 번의 날것 결과. stdout은 상한까지만 읽었다."""

    returncode: int | None
    stdout: bytes
    stderr: str
    timed_out: bool = False
    too_large: bool = False


def _run_bounded(docker_args: list[str], stdin: bytes, *, timeout: int) -> RawRun:
    """docker run을 시간과 출력 크기 안에서 돌린다. 넘으면 컨테이너를 지운다."""
    name = f"pr-proof-checker-{uuid.uuid4().hex[:12]}"
    cmd = ["docker", docker_args[0], "--name", name, *docker_args[1:]]
    timed_out = too_large = False
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=out, stderr=err, env=docker_cli.minimal_env()
            )
        except OSError as e:
            raise docker_cli.SandboxError(f"docker run 실행 실패: {e}") from e
        try:
            proc.stdin.write(stdin)
            proc.stdin.close()
        except BrokenPipeError:
            pass
        deadline = time.monotonic() + timeout
        while proc.poll() is None:
            if out.tell() > MAX_OUTPUT_BYTES:
                too_large = True
            elif time.monotonic() > deadline:
                timed_out = True
            else:
                time.sleep(POLL_SECONDS)
                continue
            docker_cli.remove_container(name)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            break
        too_large = too_large or out.tell() > MAX_OUTPUT_BYTES
        out.seek(0)
        stdout = out.read(MAX_OUTPUT_BYTES + 1)
        err.seek(0)
        stderr = err.read(64 * 1024).decode(errors="replace")
    return RawRun(proc.returncode, stdout, stderr, timed_out, too_large)


def read_result(
    kind: CheckerKind, result: RawRun, files: frozenset[str], timeout: int
) -> CheckerRun:
    """날것 결과를 읽는다. 출력 형식이 틀리거나 넘긴 파일 밖을 가리키면, 스크립트는 그 스크립트가
    틀린 것(Invalid)이고 ast-grep은 결과를 쓸 수 없는 것(Failed)이다."""
    stderr = result.stderr.strip()[-MAX_STDERR_CHARS:]
    if result.too_large:
        return Failed(f"출력이 {MAX_OUTPUT_BYTES // (1024 * 1024)}MB를 넘음")
    if result.timed_out:
        return Failed(f"{timeout}초 안에 끝나지 않음")
    if result.returncode in docker_cli.DOCKER_FAILED:
        raise docker_cli.SandboxError(f"docker run 실패 ({result.returncode}): {stderr}")
    if result.returncode == _KILLED:
        return Failed("메모리 한도를 넘었거나 강제로 끝남")
    ok = _AST_GREP_OK if kind == "ast-grep" else frozenset({0})
    if result.returncode not in ok:
        return Invalid(stderr or f"종료 코드 {result.returncode}")
    bad_output = Failed if kind == "ast-grep" else Invalid
    try:
        items = json.loads(result.stdout or b"[]")
        if not isinstance(items, list):
            raise TypeError("최상위가 목록이 아님")
        matches = tuple(
            _ast_grep_match(i) if kind == "ast-grep" else _script_match(i) for i in items
        )
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        return bad_output(f"출력이 약속한 JSON 형식이 아님: {e}")
    outside = sorted({m.file for m in matches} - files)
    if outside:
        return bad_output(f"출력이 넘기지 않은 파일을 가리킴: {', '.join(outside[:5])}")
    return Ran(matches)


def _ast_grep_match(item: dict) -> Match:
    file = item["file"]
    if not isinstance(file, str) or not file.startswith(f"{WORK}/"):
        raise ValueError(f"/work 밖의 경로: {file!r}")
    start, end = item["range"]["start"]["line"], item["range"]["end"]["line"]
    if not (isinstance(start, int) and isinstance(end, int)) or start < 0 or end < start:
        raise ValueError(f"줄 범위가 틀림: {start!r}–{end!r}")
    message = item.get("message") or ""
    return Match(_relative(file[len(WORK) + 1 :]), start + 1, end + 1, str(message))


def _script_match(item: dict) -> Match:
    file, line = item["file"], item["line"]
    if not isinstance(file, str) or not isinstance(line, int) or isinstance(line, bool):
        raise TypeError(f"file은 문자열, line은 정수여야 함: {item!r}")
    if line < 1:
        raise ValueError(f"line은 1 이상이어야 함: {line}")
    return Match(_relative(file.removeprefix("./")), line, line, str(item.get("why") or ""))


def _relative(path: str) -> str:
    if not _is_relative_inside(path):
        raise ValueError(f"상대 경로가 아님: {path!r}")
    return path
