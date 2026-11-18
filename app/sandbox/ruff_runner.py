"""샌드박스 안에서 ruff check를 실행하고 JSON 결과를 읽는다 (docs/DESIGN.md F6 lint).

ruff는 베이스 이미지에 들어 있다. 설정은 base 브랜치에서 꺼낸 파일을 `--config`로 넘긴다.
`--config`의 상대 경로(per-file-ignores, exclude)는 작업 디렉터리(/work) 기준으로 풀린다.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from app.models.text_edit import TextEdit
from app.sandbox import docker_cli
from app.sandbox.container import WORKDIR, run_in_sandbox

LINT_TIMEOUT: Final = 120
MAX_ERROR_CHARS: Final = 500
SYNTAX_ERROR_CODE: Final = "syntax-error"
SAFE_FIX: Final = "safe"  # 동작을 바꾸지 않는 수정. unsafe·display-only는 적용하지 않는다
# ruff 종료 코드: 0 위반 없음, 1 위반 있음, 2 설정·인자 오류 등
_RUFF_OK: Final = frozenset({0, 1})


@dataclass(frozen=True)
class RuffViolation:
    file: str  # 리포 기준 경로
    line: int
    end_line: int
    code: str
    message: str
    edits: tuple[TextEdit, ...] = ()  # 안전한 수정이 있을 때만. 없으면 비어 있다

    @property
    def fixable(self) -> bool:
        return bool(self.edits)


@dataclass(frozen=True)
class RuffRun:
    violations: tuple[RuffViolation, ...] = ()
    error: str | None = None  # 검사하지 못한 이유
    cancelled: bool = False


def ruff_argv(files: list[str], config: str | None, select: tuple[str, ...] = ()) -> list[str]:
    """명시한 파일도 설정의 exclude를 따르게 --force-exclude를 쓴다.
    설정이 없으면 --isolated로 ruff 기본 규칙만 쓴다 (PR head의 설정을 읽지 않게).

    select를 주면 `--select`로 규칙을 파이프라인이 정한다. 명령줄 `--select`는 설정의
    select·extend-select·ignore를 대신하지만, per-file-ignores·exclude·`# noqa`·규칙별 옵션
    (`lint.pylint.allow-magic-value-types` 등)은 설정대로 따른다 (static 스킬, DESIGN.md F6).
    """
    config_args = ["--config", config] if config is not None else ["--isolated"]
    select_args = ["--select", ",".join(select)] if select else []
    return [
        "ruff", "check", "--no-cache", "--output-format", "json", "--force-exclude",
        *config_args, *select_args, "--", *files,
    ]  # fmt: skip


def show_files_argv(files: list[str], config: str | None) -> list[str]:
    config_args = ["--config", config] if config is not None else ["--isolated"]
    return [
        "ruff", "check", "--no-cache", "--show-files", "--force-exclude", *config_args,
        "--", *files,
    ]  # fmt: skip


def _repo_path(filename: str) -> str:
    prefix = WORKDIR.rstrip("/") + "/"
    return filename[len(prefix) :] if filename.startswith(prefix) else filename


def parse_output(stdout: str) -> tuple[RuffViolation, ...]:
    """ruff JSON 출력을 읽는다. 파일 경로는 /work/ 기준이라 리포 기준으로 바꾼다."""
    violations = []
    for item in json.loads(stdout or "[]"):
        filename = _repo_path(item["filename"])
        start = item["location"]["row"]
        end = (item.get("end_location") or {}).get("row", start)
        violations.append(
            RuffViolation(
                file=filename,
                line=start,
                end_line=end,
                code=item.get("code") or SYNTAX_ERROR_CODE,
                message=item["message"],
                edits=_safe_edits(item.get("fix")),
            )
        )
    return tuple(violations)


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


FormatResult = bool | str  # True 포맷돼 있음(또는 적용함), False 아님, str 오류


def format_argv(files: list[str], config: str | None, *, check: bool) -> list[str]:
    config_args = ["--config", config] if config is not None else ["--isolated"]
    return [
        "ruff", "format", "--no-cache", "--force-exclude", *(["--check"] if check else []),
        *config_args, "--", *files,
    ]  # fmt: skip


def run_format(
    container: str,
    files: list[str],
    config: str | None,
    *,
    check: bool,
    timeout: int = LINT_TIMEOUT,
    is_stale: Callable[[], bool] | None = None,
) -> FormatResult | None:
    """`ruff format`. check면 바꾸지 않고 포맷돼 있는지만 본다. 새 커밋이 와서 중단되면 None."""
    result = run_in_sandbox(
        container, format_argv(files, config, check=check), timeout=timeout, is_stale=is_stale
    )
    if result.cancelled:
        return None
    if result.timed_out:
        return f"ruff format이 {timeout}초 안에 끝나지 않음"
    if result.returncode in docker_cli.DOCKER_FAILED:
        raise docker_cli.SandboxError(
            f"docker exec 실패 ({result.returncode}): {result.stderr.strip()[-MAX_ERROR_CHARS:]}"
        )
    if result.returncode == 0:
        return True
    if check and result.returncode == 1:
        return False  # 바꿀 파일이 있다
    return f"ruff format 오류: {result.stderr.strip()[-MAX_ERROR_CHARS:]}"


def run_ruff(
    container: str,
    files: list[str],
    config: str | None,
    *,
    select: tuple[str, ...] = (),
    timeout: int = LINT_TIMEOUT,
    is_stale: Callable[[], bool] | None = None,
) -> RuffRun:
    """바뀐 파일만 검사한다. docker 자체 실패는 SandboxError로 올린다.

    select가 비어 있으면 설정(또는 ruff 기본 규칙)의 규칙을 쓴다.
    """
    if not files:
        return RuffRun()
    result = run_in_sandbox(
        container, ruff_argv(files, config, select), timeout=timeout, is_stale=is_stale
    )
    if result.cancelled:
        return RuffRun(cancelled=True)
    if result.timed_out:
        return RuffRun(error=f"ruff가 {timeout}초 안에 끝나지 않음")
    if result.returncode in docker_cli.DOCKER_FAILED:
        raise docker_cli.SandboxError(
            f"docker exec 실패 ({result.returncode}): {result.stderr.strip()[-MAX_ERROR_CHARS:]}"
        )
    if result.returncode not in _RUFF_OK:
        detail = result.stderr.strip()[-MAX_ERROR_CHARS:] or f"종료 코드 {result.returncode}"
        return RuffRun(error=f"ruff 실행 오류: {detail}")
    try:
        return RuffRun(violations=parse_output(result.stdout))
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        return RuffRun(error=f"ruff 출력을 읽지 못함 ({e})")


ShowFilesResult = tuple[str, ...] | str  # 검사 대상 파일(리포 기준 경로), str 오류


def run_show_files(
    container: str,
    files: list[str],
    config: str | None,
    *,
    timeout: int = LINT_TIMEOUT,
    is_stale: Callable[[], bool] | None = None,
) -> ShowFilesResult | None:
    """files 중 설정의 exclude에 걸리지 않아 ruff가 검사할 파일. 새 커밋이 와서 중단되면 None.

    ruff 밖의 검사(static의 AST 규칙)도 대상 리포가 제외한 파일(생성 코드 등)을 건너뛰게 한다.
    """
    if not files:
        return ()
    result = run_in_sandbox(
        container, show_files_argv(files, config), timeout=timeout, is_stale=is_stale
    )
    if result.cancelled:
        return None
    if result.timed_out:
        return f"ruff가 {timeout}초 안에 끝나지 않음"
    if result.returncode in docker_cli.DOCKER_FAILED:
        raise docker_cli.SandboxError(
            f"docker exec 실패 ({result.returncode}): {result.stderr.strip()[-MAX_ERROR_CHARS:]}"
        )
    if result.returncode != 0:
        detail = result.stderr.strip()[-MAX_ERROR_CHARS:] or f"종료 코드 {result.returncode}"
        return f"ruff 실행 오류: {detail}"
    return tuple(sorted(_repo_path(line) for line in result.stdout.splitlines() if line.strip()))
