"""claude CLI 호출 (docs/DESIGN.md 보안·인증 > AI(claude CLI) 권한).

worktree를 cwd로 고정하고, 권한을 제한한 옵션으로 claude를 한 번 실행해 JSON 결과를 읽는다.
결과는 수정안 작성에만 쓰고 판정에는 쓰지 않는다. 모델과 호출당 예산은 호출하는 쪽이 정한다.
"""

import json
import os
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

# 호출 한 번의 시간 상한(초). 넘으면 프로세스를 죽이고 TIMEOUT으로 돌려준다
DEFAULT_TIMEOUT_S: Final = 600.0
# --max-budget-usd에 소수 넷째 자리까지 넘기므로 이보다 작으면 0이 된다
MIN_BUDGET_USD: Final = 0.0001

# 권한 제한 옵션. --restricted가 실행 도구·WebFetch를 빼고, 파일 도구를 cwd 안으로 제한하고,
# user·project·local 설정 파일(PR이 넣은 .claude/ 포함)과 CLAUDE.md를 읽지 않는다
_FIXED_ARGS: Final = (
    "-p",
    "--restricted",
    "--strict-mcp-config",
    "--permission-mode",
    "dontAsk",
    "--allowedTools",
    "Read,Grep,Glob,Write,Edit",
    "--output-format",
    "json",
)

# claude가 로컬 로그인을 찾는 데 필요한 변수만 넘긴다.
# HOME: ~/.claude(설정·세션), Linux에서는 ~/.claude/.credentials.json
# USER: macOS 키체인 항목의 계정 이름. 없으면 "Not logged in"이 된다 (2.1.327에서 확인)
# PATH: claude가 부르는 보조 명령(git 등)을 찾는다
# CLAUDE_CONFIG_DIR: 설정 폴더를 옮긴 경우에만 있다
# ANTHROPIC_API_KEY 같은 토큰과 파이프라인의 다른 환경 변수는 넘기지 않는다
_ENV_PASSTHROUGH: Final = ("HOME", "USER", "PATH", "CLAUDE_CONFIG_DIR")


class AIErrorKind(StrEnum):
    TIMEOUT = "timeout"
    LAUNCH = "launch"  # claude를 실행하지 못함 (명령 없음, worktree 없음)
    EXIT = "exit"  # 0이 아닌 종료 코드나 is_error (로그인 풀림 포함)
    BAD_JSON = "bad_json"
    BUDGET = "budget"  # --max-budget-usd를 넘김


@dataclass(frozen=True)
class PermissionDenial:
    """허용 범위 밖이라 거부된 도구 호출 한 건 (worktree 밖 Read 등)."""

    tool_name: str
    tool_input: Mapping[str, object]


@dataclass(frozen=True)
class ClaudeResult:
    """claude 호출 결과. ok는 호출이 정상으로 끝났다는 뜻일 뿐, 수정안의 판정이 아니다.

    cost_usd가 None이면 비용을 알 수 없다 (시간 초과, JSON 없음). 예산 계산에서는 호출당 상한을
    쓴 것으로 보면 된다.
    """

    ok: bool
    error: AIErrorKind | None
    cost_usd: float | None
    num_turns: int
    permission_denials: tuple[PermissionDenial, ...]
    text: str
    detail: str = ""


# (args, 표준 입력, cwd, env, timeout) → 완료된 프로세스. 시간 초과는 TimeoutExpired로 올린다
Runner = Callable[[list[str], str, Path, dict[str, str], float], subprocess.CompletedProcess[str]]


def claude_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """claude가 로그인을 찾는 데 필요한 변수만 고른다."""
    source = os.environ if environ is None else environ
    return {k: source[k] for k in _ENV_PASSTHROUGH if k in source}


def build_args(model: str, max_budget_usd: float) -> list[str]:
    """claude 명령 인자. 프롬프트는 인자가 아니라 표준 입력으로 넘긴다."""
    if not model or model.startswith("-"):
        raise ValueError(f"잘못된 모델 이름: {model!r}")
    if not max_budget_usd >= MIN_BUDGET_USD:
        raise ValueError(f"호출당 예산은 {MIN_BUDGET_USD} 이상이어야 한다: {max_budget_usd}")
    return [
        "claude",
        *_FIXED_ARGS,
        "--model",
        model,
        "--max-budget-usd",
        f"{max_budget_usd:.4f}",
    ]


def _run(
    args: list[str], stdin: str, cwd: Path, env: dict[str, str], timeout: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        input=stdin,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def run_claude(
    worktree: Path,
    prompt: str,
    *,
    model: str,
    max_budget_usd: float,
    timeout: float = DEFAULT_TIMEOUT_S,
    runner: Runner = _run,
    environ: Mapping[str, str] | None = None,
) -> ClaudeResult:
    """worktree를 cwd로 claude를 한 번 실행한다. 셸을 거치지 않고, 프롬프트는 표준 입력으로 준다."""
    args = build_args(model, max_budget_usd)
    try:
        proc = runner(args, prompt, worktree, claude_env(environ), timeout)
    except subprocess.TimeoutExpired:
        return _failure(AIErrorKind.TIMEOUT, f"{timeout:g}초 안에 끝나지 않음")
    except OSError as e:
        return _failure(AIErrorKind.LAUNCH, str(e))
    return parse_output(proc.stdout, proc.returncode, proc.stderr)


def parse_output(stdout: str, returncode: int, stderr: str = "") -> ClaudeResult:
    """`--output-format json` 출력을 읽는다. 실패해도 JSON에 있는 비용과 거부 목록은 남긴다."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict):
        return _failure(AIErrorKind.BAD_JSON, _tail(stderr or stdout), returncode=returncode)

    cost = data.get("total_cost_usd")
    turns = data.get("num_turns")
    text = data.get("result")
    subtype = str(data.get("subtype", ""))
    error: AIErrorKind | None = None
    if "budget" in subtype:
        error = AIErrorKind.BUDGET
    elif returncode != 0 or data.get("is_error") is True:
        error = AIErrorKind.EXIT
    return ClaudeResult(
        ok=error is None,
        error=error,
        cost_usd=float(cost) if isinstance(cost, int | float) else None,
        num_turns=turns if isinstance(turns, int) else 0,
        permission_denials=_denials(data.get("permission_denials")),
        text=text if isinstance(text, str) else "",
        detail="" if error is None else f"종료 코드 {returncode}, subtype {subtype}",
    )


def _denials(raw: object) -> tuple[PermissionDenial, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(
        PermissionDenial(
            tool_name=str(d.get("tool_name", "")),
            tool_input=d["tool_input"] if isinstance(d.get("tool_input"), dict) else {},
        )
        for d in raw
        if isinstance(d, dict)
    )


def _failure(kind: AIErrorKind, detail: str, returncode: int | None = None) -> ClaudeResult:
    if returncode is not None:
        detail = f"종료 코드 {returncode}: {detail}"
    return ClaudeResult(
        ok=False,
        error=kind,
        cost_usd=None,
        num_turns=0,
        permission_denials=(),
        text="",
        detail=detail,
    )


def _tail(text: str, limit: int = 500) -> str:
    return text.strip()[-limit:]
