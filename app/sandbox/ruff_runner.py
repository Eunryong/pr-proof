"""ruff를 샌드박스에서 돌린다 (docs/DESIGN.md 4.4 (a) 도구, 4.5 도구와 검사기, 4.7 기계적 수정).

ruff는 이미지에 없다. 도구 캐시(sandbox/toolcache.py)에 받은 실행 파일을 읽기 전용으로 붙인다.
컨테이너 하나에 명령 하나다: 네트워크 없음, 루트 파일 시스템 읽기 전용, 권한 없음, 자원 제한, 호스트
UID. 검사할 폴더는 읽기 전용이고 고치는 `format_apply`만 쓰기를 연다.

설정은 base 커밋에서 읽은 파일을 검사할 폴더 밖에 두고 `--config`로 넘긴다. PR이 넣은 설정 파일은
읽히지 않고(`--config`는 설정 찾기를 끈다), PR 코드가 그 사본을 바꿀 수도 없다. 설정이 없으면
`--isolated`다. `--config`의 상대 경로(per-file-ignores, exclude)는 작업 폴더(/work) 기준으로
풀린다.
파일 경로는 `--` 뒤에만 둔다 (T9).

출력은 믿지 않는 입력이다. 크기 상한 안의 JSON·diff만 읽고, 넘긴 파일 밖을 가리키면 결과를 버린다.
"""

import json
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from app.models.text_edit import TextEdit
from app.models.tools import RUFF_CODE, RuffConfig
from app.sandbox import docker_cli
from app.sandbox.checker_runner import (
    MAX_STDERR_CHARS,
    CheckerRun,
    Failed,
    Match,
    Ran,
    RawRun,
    check_paths,
    run_bounded,
)

WORK: Final = "/work"
TOOL_MOUNT: Final = "/tools/ruff"
CONFIG_MOUNT: Final = "/config"
RUFF_TIMEOUT: Final = 120
SYNTAX_ERROR_CODE: Final = "syntax-error"
SAFE_FIX: Final = "safe"  # 동작을 바꾸지 않는 수정. unsafe·display-only는 적용하지 않는다
FORMAT_MESSAGE: Final = "ruff format이 이 줄을 바꾼다"
# ruff-format이 찾은 위반의 코드. ruff의 규칙 코드가 아니다
FORMAT_CODE: Final = "format"
# ruff 종료 코드: 0 위반 없음(포맷 그대로), 1 위반 있음(포맷이 바뀜), 2 설정·인자 오류 등
_OK: Final = frozenset({0, 1})
_KILLED: Final = 137

# (포맷돼 있음 또는 적용함) True, (포맷돼 있지 않음) False, 오류는 그 이유
FormatResult = bool | str


@contextmanager
def _container(
    root: Path,
    *,
    tool_dir: Path,
    config: RuffConfig | None,
    writable: bool = False,
    memory_gb: float = docker_cli.MEMORY_LIMIT_GB,
) -> Iterator[tuple[list[str], list[str]]]:
    """(docker 인자, ruff 설정 인자). 설정 사본은 검사할 폴더 밖의 임시 폴더에 둔다."""
    with tempfile.TemporaryDirectory(prefix="pipeline-ruff-") as tmp:
        mounts = ["-v", f"{root.resolve()}:{WORK}{'' if writable else ':ro'}",
                  "-v", f"{tool_dir.resolve()}:{TOOL_MOUNT}:ro"]  # fmt: skip
        if config is not None:
            (Path(tmp) / config.name).write_text(config.text)
            mounts += ["-v", f"{tmp}:{CONFIG_MOUNT}:ro"]
            config_args = ["--config", f"{CONFIG_MOUNT}/{config.name}"]
        else:
            config_args = ["--isolated"]
        docker = [
            "run", "--rm", "-i", "--network", "none", "--read-only", *docker_cli.limits(memory_gb),
            "--user", docker_cli.user(), "-e", "HOME=/tmp", *mounts, "-w", WORK,
            docker_cli.TOOL_IMAGE, f"{TOOL_MOUNT}/ruff",
        ]  # fmt: skip
        yield docker, config_args


def _failure(result: RawRun, timeout: int, what: str) -> CheckerRun | None:
    """결과를 쓸 수 없으면 그 이유. docker 자체가 실패했으면 SandboxError (환경 오류)."""
    stderr = result.stderr.strip()[-MAX_STDERR_CHARS:]
    if result.too_large:
        return Failed(f"{what} 출력이 크기 상한을 넘음")
    if result.timed_out:
        return Failed(f"{what}이 {timeout}초 안에 끝나지 않음")
    if result.returncode in docker_cli.DOCKER_FAILED:
        raise docker_cli.SandboxError(f"docker run 실패 ({result.returncode}): {stderr}")
    if result.returncode == _KILLED:
        return Failed(f"{what}이 메모리 한도를 넘었거나 강제로 끝남")
    if result.returncode not in _OK:
        return Failed(f"{what} 실행 오류: {stderr or f'종료 코드 {result.returncode}'}")
    return None


def check(
    root: Path,
    files: Sequence[str],
    *,
    tool_dir: Path,
    config: RuffConfig | None,
    codes: Sequence[str] = (),
    timeout: int = RUFF_TIMEOUT,
) -> CheckerRun:
    """root 아래 files에 `ruff check`를 돌린다. codes가 있으면 그 규칙만(`--select`), 없으면 설정의
    규칙이다. 명시한 파일도 설정의 exclude를 따르게 `--force-exclude`를 쓴다."""
    check_paths(files)
    if not files:
        return Ran(())
    select = ["--select", ",".join(codes)] if codes else []
    with _container(root, tool_dir=tool_dir, config=config) as (docker, config_args):
        argv = [*docker, "check", "--no-cache", "--output-format", "json", "--force-exclude",
                *config_args, *select, "--", *files]  # fmt: skip
        result = run_bounded(argv, b"", timeout=timeout)
    failed = _failure(result, timeout, "ruff")
    if failed is not None:
        return failed
    try:
        matches = parse_check(result.stdout.decode(errors="replace"))
    except (ValueError, KeyError, TypeError) as e:
        return Failed(f"ruff 출력을 읽지 못함 ({e})")
    return _inside(matches, files)


def _inside(matches: tuple[Match, ...], files: Sequence[str]) -> CheckerRun:
    outside = sorted({m.file for m in matches} - set(files))
    if outside:
        return Failed(f"출력이 넘기지 않은 파일을 가리킴: {', '.join(outside[:5])}")
    return Ran(matches)


def _repo_path(filename: str) -> str:
    prefix = WORK + "/"
    return filename[len(prefix) :] if filename.startswith(prefix) else filename


def parse_check(stdout: str) -> tuple[Match, ...]:
    """ruff JSON 출력을 읽는다. 파일 경로는 /work/ 기준이라 저장소 기준으로 바꾼다."""
    items = json.loads(stdout or "[]")
    if not isinstance(items, list):
        raise TypeError("최상위가 목록이 아님")
    out = []
    for item in items:
        start = item["location"]["row"]
        end = (item.get("end_location") or {}).get("row", start)
        if not (isinstance(start, int) and isinstance(end, int)) or start < 1 or end < start:
            raise ValueError(f"줄 범위가 틀림: {start!r}–{end!r}")
        out.append(
            Match(
                file=_repo_path(item["filename"]),
                line=start,
                end_line=end,
                message=str(item["message"]),
                code=item.get("code") or SYNTAX_ERROR_CODE,
                edits=_safe_edits(item.get("fix")),
            )
        )
    return tuple(out)


def _safe_edits(fix: dict | None) -> tuple[TextEdit, ...]:
    """안전한 수정의 편집 목록. 위치는 1부터 세는 줄·열(문자 단위)이다."""
    if not fix or fix.get("applicability") != SAFE_FIX:
        return ()
    return tuple(
        TextEdit(
            row=e["location"]["row"],
            column=e["location"]["column"],
            end_row=e["end_location"]["row"],
            end_column=e["end_location"]["column"],
            content=e.get("content") or "",
        )
        for e in fix.get("edits") or ()
    )


def format_diff(
    root: Path,
    files: Sequence[str],
    *,
    tool_dir: Path,
    config: RuffConfig | None,
    timeout: int = RUFF_TIMEOUT,
) -> CheckerRun:
    """`ruff format --diff`가 바꿀 줄. 바꿀 덩어리마다 Match 하나다 (원래 파일의 줄 번호)."""
    check_paths(files)
    if not files:
        return Ran(())
    with _container(root, tool_dir=tool_dir, config=config) as (docker, config_args):
        argv = [*docker, "format", "--no-cache", "--force-exclude", "--diff", *config_args, "--",
                *files]  # fmt: skip
        result = run_bounded(argv, b"", timeout=timeout)
    failed = _failure(result, timeout, "ruff format")
    if failed is not None:
        return failed
    return _inside(parse_format_diff(result.stdout.decode(errors="replace")), files)


def parse_format_diff(diff: str) -> tuple[Match, ...]:
    """unified diff에서 원래 파일의 바뀌는 줄 범위. 줄을 더하기만 하는 덩어리는 그 앞줄이다."""
    out: list[Match] = []
    file = ""
    old = 0
    changed: list[int] = []

    def close() -> None:
        if file and changed:
            out.append(Match(file, min(changed), max(changed), FORMAT_MESSAGE, FORMAT_CODE))
        changed.clear()

    for line in diff.splitlines():
        if line.startswith("--- "):
            close()
            file = _repo_path(line[4:].split("\t")[0].strip())
        elif line.startswith("+++ "):
            continue
        elif line.startswith("@@"):
            close()
            old = int(line.split()[1].lstrip("-").split(",")[0])
        elif line.startswith("-"):
            changed.append(old)
            old += 1
        elif line.startswith("+"):
            changed.append(max(1, old - 1))
        elif line.startswith(" "):
            close()
            old += 1
    close()
    return tuple(out)


def format_check(
    root: Path, *, tool_dir: Path, config: RuffConfig | None, timeout: int = RUFF_TIMEOUT
) -> FormatResult:
    """root 전체가 `ruff format --check`를 통과하는지 (4.7: base가 포맷돼 있을 때만 포맷한다)."""
    with _container(root, tool_dir=tool_dir, config=config) as (docker, config_args):
        argv = [*docker, "format", "--no-cache", "--force-exclude", "--check", *config_args, "--",
                "."]  # fmt: skip
        result = run_bounded(argv, b"", timeout=timeout)
    failed = _failure(result, timeout, "ruff format")
    if isinstance(failed, Failed):
        return failed.reason
    return result.returncode == 0


def format_apply(
    root: Path,
    files: Sequence[str],
    *,
    tool_dir: Path,
    config: RuffConfig | None,
    timeout: int = RUFF_TIMEOUT,
) -> FormatResult:
    """files를 `ruff format`으로 고친다. root를 쓰기로 붙이는 유일한 실행이다."""
    check_paths(files)
    if not files:
        return True
    with _container(root, tool_dir=tool_dir, config=config, writable=True) as (docker, args):
        argv = [*docker, "format", "--no-cache", "--force-exclude", *args, "--", *files]
        result = run_bounded(argv, b"", timeout=timeout)
    failed = _failure(result, timeout, "ruff format")
    if isinstance(failed, Failed):
        return failed.reason
    return result.returncode == 0 or f"ruff format 종료 코드 {result.returncode}"


def rule_exists(code: str, *, tool_dir: Path, timeout: int = 30) -> bool:
    """`ruff rule <코드>`가 그 규칙을 아는지 (4.4: 모델이 고른 코드가 실재하는지)."""
    if not RUFF_CODE.match(code):  # 형식이 맞는 코드만 인자로 넘긴다 (`-`로 시작하는 옵션이 아님)
        return False
    with tempfile.TemporaryDirectory(prefix="pipeline-ruff-") as tmp:
        with _container(Path(tmp), tool_dir=tool_dir, config=None) as (docker, _):
            result = run_bounded([*docker, "rule", code], b"", timeout=timeout)
    if result.returncode in docker_cli.DOCKER_FAILED:
        raise docker_cli.SandboxError(f"docker run 실패 ({result.returncode}): {result.stderr}")
    return result.returncode == 0 and not result.timed_out
