"""샌드박스 안에서 ruff check를 실행하고 JSON 결과를 읽는다 (docs/DESIGN.md F6 lint).

ruff는 베이스 이미지에 들어 있다. 설정은 base 브랜치에서 꺼낸 파일을 `--config`로 넘긴다.
`--config`의 상대 경로(per-file-ignores, exclude)는 작업 디렉터리(/work) 기준으로 풀린다.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from app.sandbox import docker_cli
from app.sandbox.container import WORKDIR, run_in_sandbox

LINT_TIMEOUT: Final = 120
MAX_ERROR_CHARS: Final = 500
SYNTAX_ERROR_CODE: Final = "syntax-error"
# ruff 종료 코드: 0 위반 없음, 1 위반 있음, 2 설정·인자 오류 등
_RUFF_OK: Final = frozenset({0, 1})


@dataclass(frozen=True)
class RuffViolation:
    file: str  # 리포 기준 경로
    line: int
    end_line: int
    code: str
    message: str


@dataclass(frozen=True)
class RuffRun:
    violations: tuple[RuffViolation, ...] = ()
    error: str | None = None  # 검사하지 못한 이유
    cancelled: bool = False


def ruff_argv(files: list[str], config: str | None) -> list[str]:
    """명시한 파일도 설정의 exclude를 따르게 --force-exclude를 쓴다.
    설정이 없으면 --isolated로 ruff 기본 규칙만 쓴다 (PR head의 설정을 읽지 않게)."""
    config_args = ["--config", config] if config is not None else ["--isolated"]
    return [
        "ruff", "check", "--no-cache", "--output-format", "json", "--force-exclude",
        *config_args, "--", *files,
    ]  # fmt: skip


def parse_output(stdout: str) -> tuple[RuffViolation, ...]:
    """ruff JSON 출력을 읽는다. 파일 경로는 /work/ 기준이라 리포 기준으로 바꾼다."""
    prefix = WORKDIR.rstrip("/") + "/"
    violations = []
    for item in json.loads(stdout or "[]"):
        filename = item["filename"]
        if filename.startswith(prefix):
            filename = filename[len(prefix) :]
        start = item["location"]["row"]
        end = (item.get("end_location") or {}).get("row", start)
        violations.append(
            RuffViolation(
                file=filename,
                line=start,
                end_line=end,
                code=item.get("code") or SYNTAX_ERROR_CODE,
                message=item["message"],
            )
        )
    return tuple(violations)


def run_ruff(
    container: str,
    files: list[str],
    config: str | None,
    *,
    timeout: int = LINT_TIMEOUT,
    is_stale: Callable[[], bool] | None = None,
) -> RuffRun:
    """바뀐 파일만 검사한다. docker 자체 실패는 SandboxError로 올린다."""
    if not files:
        return RuffRun()
    result = run_in_sandbox(container, ruff_argv(files, config), timeout=timeout, is_stale=is_stale)
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
