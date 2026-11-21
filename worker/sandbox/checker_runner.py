"""검사기를 샌드박스에서 돌리고 결과를 읽는다 (docs/DESIGN.md 4.4 예시 확인, 4.5, 8.1).

검사기 하나를 컨테이너 하나에서 돌린다. 네트워크 없음, 루트 파일 시스템 읽기 전용, 권한 없음,
자원 제한, 호스트 UID. 검사할 폴더(worktree 또는 예시 폴더), 검사기 파일, 도구 캐시는 모두 읽기
전용이다. 이미지에는 언어가 없어서 ast-grep은 그 실행 파일을, Python AST 스크립트와 경로 패턴은
Python 런타임을 도구 캐시에서 붙인다. 작업 디렉터리는 검사기 폴더라서 검사할 폴더의
`sgconfig.yml`을 읽지 않는다 (PR이 넣은 설정이 언어 연결을 바꾸면 위반이 숨는다).

출력은 믿지 않는 입력이다. 크기 상한 안의 JSON만 읽고, 넘긴 파일 밖을 가리키는 항목이 있으면
결과 전체를 버린다.
"""

import json
import subprocess
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from worker.checker_pure import CheckerKind, is_repo_path, validate_body
from worker.sandbox import docker_cli, toolcache
from worker.text_edit_pure import TextEdit

WORK: Final = "/work"
CHECKER_DIR: Final = "/checker"
TOOL_MOUNT: Final = "/tools/ast-grep"
PYTHON_MOUNT: Final = "/tools/python"
PYTHON_BIN: Final = f"{PYTHON_MOUNT}/{toolcache.TOOLS[toolcache.PYTHON].binary}"
RULE_FILE: Final = "rule.yml"
SCRIPT_FILE: Final = "check.py"
EXAMPLE_DIR: Final = "examples"

CHECK_TIMEOUT: Final = 120
# 경로 패턴은 정규식 몇 번이다. 오래 걸리면 정규식이 나쁜 것이다 (끝없는 되짚기)
PATH_PATTERN_TIMEOUT: Final = 20
# 경로 패턴 선언을 경로 목록에 맞추는 스크립트. 모델이 쓴 것은 선언(자료)뿐이고 이 코드는 고정이다.
# 표준 입력: {"decl": {...}, "paths": [...]}. applies_to가 맞는 경로 중 조건을 어긴 것을 낸다
_PATH_SCRIPT: Final = """\
import json, re, sys
data = json.load(sys.stdin)
decl = data["decl"]
applies = re.compile(decl["applies_to"]) if decl.get("applies_to") else None
pattern = re.compile(decl["pattern"])
want = decl["condition"] == "must_match"
out = []
for path in data["paths"]:
    if applies is not None and not applies.search(path):
        continue
    if bool(pattern.search(path)) != want:
        out.append({"file": path, "line": 1, "why": decl.get("message") or ""})
json.dump(out, sys.stdout)
"""
# PR 패턴 선언(4.5 끝)을 PR 사실에 맞추는 스크립트. 모델이 쓴 것은 선언뿐이고 이 코드는 고정이다
# (models/pr_check.py의 ENGINE_VERSION). 표준 입력: {"decl": {...}, "cases": [{"values", "paths"}]}.
# 사례마다 바뀐 파일 조건이 맞았는지(gated)와 조건을 어긴 값의 순번(bad)을 낸다
_PR_SCRIPT: Final = """\
import json, re, sys
data = json.load(sys.stdin)
decl = data["decl"]
gate = re.compile(decl["when_changed"]) if decl["when_changed"] else None
pattern = re.compile(decl["pattern"]) if decl["pattern"] else None
want = decl["condition"] == "must_match"
out = []
for case in data["cases"]:
    gated = gate is None or any(gate.search(p) for p in case["paths"])
    bad = []
    if gated and pattern is not None:
        if decl["on_paths"]:
            if any(pattern.search(p) for p in case["paths"]) != want:
                bad = [0]
        else:
            bad = [i for i, v in enumerate(case["values"]) if bool(pattern.search(v)) != want]
    out.append({"gated": gated, "bad": bad})
json.dump(out, sys.stdout)
"""
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
    message: str  # ast-grep 규칙의 message, 스크립트의 why, ruff의 메시지
    code: str = ""  # ruff의 규칙 코드
    edits: tuple[TextEdit, ...] = ()  # ruff의 안전한 수정. 없으면 비어 있다


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
    tool_dir: Path,
    image: str,
    timeout: int = CHECK_TIMEOUT,
    memory_gb: float = docker_cli.MEMORY_LIMIT_GB,
) -> CheckerRun:
    """root 아래 files(상대 경로)에 검사기를 image 컨테이너에서 돌린다. tool_dir은 도구 캐시의
    폴더다: ast-grep은 ast-grep, Python AST 스크립트와 경로 패턴은 Python 런타임.

    docker 자체가 실패하면 SandboxError를 올린다.
    """
    if kind == "path-pattern":
        return run_path_pattern(body, files, tool_dir=tool_dir, image=image)
    check_paths(files)
    problem = validate_body(kind, body)
    if problem:
        return Invalid(problem)
    if not files:
        return Ran(())
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
            mounts = ("-v", f"{tool_dir.resolve()}:{PYTHON_MOUNT}:ro")
            argv = [PYTHON_BIN, "-I", f"{CHECKER_DIR}/{SCRIPT_FILE}"]
            stdin = json.dumps({"files": list(files)}).encode()
        workdir = CHECKER_DIR if kind == "ast-grep" else WORK
        docker = [
            "run", "--rm", "-i", "--network", "none", "--read-only", *docker_cli.limits(memory_gb),
            "--user", docker_cli.user(), "-e", "HOME=/tmp", "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-v", f"{root.resolve()}:{WORK}:ro", "-v", f"{checker_dir}:{CHECKER_DIR}:ro", *mounts,
            "-w", workdir, image, *argv,
        ]  # fmt: skip
        result = run_bounded(docker, stdin, timeout=timeout)
    return read_result(kind, result, frozenset(files), timeout)


def run_on_examples(
    kind: CheckerKind,
    body: str,
    snippets: Sequence[str],
    *,
    tool_dir: Path,
    image: str,
    timeout: int = CHECK_TIMEOUT,
) -> CheckerRun:
    """예시 코드마다 파일을 하나씩 만들어 검사기를 돌린다. 저장소는 마운트하지 않는다.

    Match.file은 `example_file(kind, i, snippet)`이다. 경로 패턴의 예시는 코드가 아니라 경로라서
    파일을 만들지 않는다.
    """
    if kind == "path-pattern":
        return run_path_pattern(body, [s.strip() for s in snippets], tool_dir=tool_dir, image=image)
    with tempfile.TemporaryDirectory(prefix="pipeline-examples-") as tmp:
        root = Path(tmp)
        (root / EXAMPLE_DIR).mkdir()
        for i, code in enumerate(snippets):
            (root / example_path(i)).write_text(code)
        files = [example_path(i) for i in range(len(snippets))]
        return run_checker(kind, body, root, files, tool_dir=tool_dir, image=image, timeout=timeout)


def example_path(index: int) -> str:
    return f"{EXAMPLE_DIR}/example_{index}.py"


def example_file(kind: CheckerKind, index: int, snippet: str) -> str:
    """예시 index의 결과가 달리는 Match.file."""
    return snippet.strip() if kind == "path-pattern" else example_path(index)


def run_path_pattern(
    body: str,
    paths: Sequence[str],
    *,
    tool_dir: Path,
    image: str,
    timeout: int = PATH_PATTERN_TIMEOUT,
) -> CheckerRun:
    """경로 패턴 선언을 paths(저장소 기준 경로)에 맞춘다. 파일은 읽지 않고 저장소도 마운트하지
    않는다. 위반은 파일 단위다 (Match.line은 1). tool_dir은 도구 캐시의 Python 런타임이다.

    정규식은 모델이 쓴 것이라 끝없이 되짚을 수 있다. 그래서 호스트가 아니라 시간·자원 제한이 있는
    컨테이너에서 맞춘다.
    """
    check_paths(paths)
    problem = validate_body("path-pattern", body)
    if problem:
        return Invalid(problem)
    if not paths:
        return Ran(())
    with tempfile.TemporaryDirectory(prefix="pipeline-checker-") as tmp:
        checker_dir = Path(tmp)
        (checker_dir / SCRIPT_FILE).write_text(_PATH_SCRIPT)
        stdin = json.dumps({"decl": json.loads(body), "paths": list(paths)}).encode()
        docker = [
            "run", "--rm", "-i", "--network", "none", "--read-only", *docker_cli.limits(),
            "--user", docker_cli.user(), "-e", "HOME=/tmp", "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-v", f"{checker_dir}:{CHECKER_DIR}:ro",
            "-v", f"{tool_dir.resolve()}:{PYTHON_MOUNT}:ro",
            "-w", CHECKER_DIR, image, PYTHON_BIN, "-I", f"{CHECKER_DIR}/{SCRIPT_FILE}",
        ]  # fmt: skip
        result = run_bounded(docker, stdin, timeout=timeout)
    return read_result("path-pattern", result, frozenset(paths), timeout)


@dataclass(frozen=True)
class PrRan:
    """PR 패턴의 고정 스크립트가 끝까지 돌았다. items는 사례마다의 날것 결과다(형식 확인은
    models/pr_check.read_results)."""

    items: object


def run_pr_pattern(
    decl: Mapping[str, object],
    cases: Sequence[Mapping[str, object]],
    *,
    tool_dir: Path,
    image: str,
    timeout: int = PATH_PATTERN_TIMEOUT,
) -> PrRan | Failed:
    """PR 패턴 선언(models/pr_check.PrPattern.script_input)을 사례들(예시, 또는 이 PR의 사실)에
    맞춘다 (4.5 끝). 사례의 값(커밋 메시지, PR 본문)은 PR 작성자의 글이고 정규식은 모델이 쓴
    것이다. 그래서 호스트가 아니라 네트워크 없고 시간·자원 제한이 있는 컨테이너에서 맞춘다. 저장소는
    마운트하지 않는다. 시간을 넘기면(끝없는 되짚기) Failed다."""
    with tempfile.TemporaryDirectory(prefix="pipeline-pr-pattern-") as tmp:
        checker_dir = Path(tmp)
        (checker_dir / SCRIPT_FILE).write_text(_PR_SCRIPT)
        stdin = json.dumps({"decl": dict(decl), "cases": [dict(c) for c in cases]}).encode()
        docker = [
            "run", "--rm", "-i", "--network", "none", "--read-only", *docker_cli.limits(),
            "--user", docker_cli.user(), "-e", "HOME=/tmp", "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-v", f"{checker_dir}:{CHECKER_DIR}:ro",
            "-v", f"{tool_dir.resolve()}:{PYTHON_MOUNT}:ro",
            "-w", CHECKER_DIR, image, PYTHON_BIN, "-I", f"{CHECKER_DIR}/{SCRIPT_FILE}",
        ]  # fmt: skip
        result = run_bounded(docker, stdin, timeout=timeout)
    stderr = result.stderr.strip()[-MAX_STDERR_CHARS:]
    if result.too_large:
        return Failed(f"출력이 {MAX_OUTPUT_BYTES // (1024 * 1024)}MB를 넘음")
    if result.timed_out:
        return Failed(f"정규식이 {timeout}초 안에 끝나지 않음")
    if result.returncode in docker_cli.DOCKER_FAILED:
        raise docker_cli.SandboxError(f"docker run 실패 ({result.returncode}): {stderr}")
    if result.returncode == _KILLED:
        return Failed("메모리 한도를 넘었거나 강제로 끝남")
    if result.returncode != 0:
        return Failed(stderr or f"종료 코드 {result.returncode}")
    try:
        return PrRan(json.loads(result.stdout or b"[]"))
    except ValueError as e:
        return Failed(f"출력이 JSON이 아님: {e}")


def check_paths(files: Sequence[str]) -> None:
    for f in files:
        if not _is_relative_inside(f):
            raise ValueError(f"검사할 파일은 root 안의 상대 경로여야 한다: {f!r}")


def _is_relative_inside(path: str) -> bool:
    return is_repo_path(path)


@dataclass(frozen=True)
class RawRun:
    """docker run 한 번의 날것 결과. stdout은 상한까지만 읽었다."""

    returncode: int | None
    stdout: bytes
    stderr: str
    timed_out: bool = False
    too_large: bool = False


def run_bounded(docker_args: list[str], stdin: bytes, *, timeout: int) -> RawRun:
    """docker run을 시간과 출력 크기 안에서 돌린다. 넘으면 컨테이너를 지운다."""
    name = f"pr-proof-checker-{uuid.uuid4().hex[:12]}"
    cmd = ["docker", docker_args[0], "--name", name, *docker_args[1:]]
    timed_out = too_large = False
    # 표준 입력은 파일로 넘긴다. 파이프면 입력을 읽지 않는 스크립트 앞에서 쓰기가 멈춘다
    with (
        tempfile.TemporaryFile() as given,
        tempfile.TemporaryFile() as out,
        tempfile.TemporaryFile() as err,
    ):
        given.write(stdin)
        given.seek(0)
        try:
            proc = subprocess.Popen(
                cmd, stdin=given, stdout=out, stderr=err, env=docker_cli.minimal_env()
            )
        except OSError as e:
            raise docker_cli.SandboxError(f"docker run 실행 실패: {e}") from e
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
