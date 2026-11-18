"""claude CLI 호출 (docs/DESIGN.md 8.4).

호출 모양은 둘이다. 수정 작성은 worktree를 cwd로 파일 도구 다섯 개만 주고(`run_claude`), 나머지
단계는 빈 임시 폴더를 cwd로 도구 없이 부르고 구조화 출력을 받는다(`run_structured`). 둘 다
권한을 제한한 같은 옵션 위에 모양별 옵션을 더한다. 모델과 호출당 예산은 호출하는 쪽이 정한다.
"""

import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, Literal

# 호출 한 번의 시간 상한(초). 넘으면 프로세스를 죽이고 TIMEOUT으로 돌려준다
DEFAULT_TIMEOUT_S: Final = 600.0
# --max-budget-usd에 소수 넷째 자리까지 넘기므로 이보다 작으면 0이 된다
MIN_BUDGET_USD: Final = 0.0001

# 권한 제한 옵션. --restricted가 실행 도구·WebFetch를 빼고, 파일 도구를 cwd 안으로 제한하고,
# user·project·local 설정 파일(PR이 넣은 .claude/ 포함)과 CLAUDE.md를 읽지 않는다.
# --allowedTools는 승인만 하고 도구 목록을 줄이지 않으므로(2.1.327에서 WebSearch, Task 등 21개가
# 보였다) 목록은 --tools로 정한다. 스킬·명령은 끄고, 세션 기록은 ~/.claude에 남기지 않는다
_COMMON_ARGS: Final = (
    "-p",
    "--restricted",
    "--strict-mcp-config",
    "--permission-mode",
    "dontAsk",
    "--disable-slash-commands",
    "--no-session-persistence",
    "--output-format",
    "json",
)
FILE_TOOLS: Final = "Read,Grep,Glob,Write,Edit"
CallShape = Literal["file_tools", "no_tools"]
_SHAPE_ARGS: Final[Mapping[str, tuple[str, ...]]] = {
    "file_tools": ("--tools", FILE_TOOLS, "--allowedTools", FILE_TOOLS),
    "no_tools": ("--tools", ""),
}

# claude가 로컬 로그인을 찾는 데 필요한 변수만 넘긴다.
# HOME: ~/.claude(설정·세션), Linux에서는 ~/.claude/.credentials.json
# USER: macOS 키체인 항목의 계정 이름. 없으면 "Not logged in"이 된다 (2.1.327에서 확인)
# PATH: claude가 부르는 보조 명령(git 등)을 찾는다
# CLAUDE_CONFIG_DIR: 설정 폴더를 옮긴 경우에만 있다
# ANTHROPIC_API_KEY 같은 토큰과 파이프라인의 다른 환경 변수는 넘기지 않는다
_ENV_PASSTHROUGH: Final = ("HOME", "USER", "PATH", "CLAUDE_CONFIG_DIR")
# 실제 입력 토큰은 셋의 합이다. input_tokens만 보면 캐시를 쓴 호출에서 2 같은 값이 나온다
_INPUT_TOKEN_KEYS: Final = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


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


def build_args(
    shape: CallShape, model: str, max_budget_usd: float, extra: tuple[str, ...] = ()
) -> list[str]:
    """claude 명령 인자. 프롬프트는 인자가 아니라 표준 입력으로 넘긴다."""
    if not model or model.startswith("-"):
        raise ValueError(f"잘못된 모델 이름: {model!r}")
    if not max_budget_usd >= MIN_BUDGET_USD:
        raise ValueError(f"호출당 예산은 {MIN_BUDGET_USD} 이상이어야 한다: {max_budget_usd}")
    return [
        "claude",
        *_COMMON_ARGS,
        *_SHAPE_ARGS[shape],
        *extra,
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
    """worktree를 cwd로 파일 도구만 준 claude를 한 번 실행한다. 셸을 거치지 않는다."""
    args = build_args("file_tools", model, max_budget_usd)
    try:
        proc = runner(args, prompt, worktree, claude_env(environ), timeout)
    except subprocess.TimeoutExpired:
        return _failure(AIErrorKind.TIMEOUT, f"{timeout:g}초 안에 끝나지 않음")
    except OSError as e:
        return _failure(AIErrorKind.LAUNCH, str(e))
    return parse_output(proc.stdout, proc.returncode, proc.stderr)


@dataclass(frozen=True)
class StructuredResult:
    """도구 없는 호출의 결과. ok이면 data에 스키마로 받은 객체가 있다 (형식 확인은 부른 쪽이 한다).

    model_id는 별칭이 가리킨 전체 모델 id(`modelUsage`의 키)다. 모르면 "".
    input_tokens는 캐시 생성·읽기를 합친 실제 입력이다. 모르면 None.
    """

    ok: bool
    error: AIErrorKind | None
    data: Mapping[str, object] | None
    model_id: str
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    permission_denials: int
    detail: str = ""


def run_structured(
    prompt: str,
    *,
    system_prompt: str,
    schema: Mapping[str, object],
    model: str,
    max_budget_usd: float,
    timeout: float = DEFAULT_TIMEOUT_S,
    runner: Runner = _run,
    environ: Mapping[str, str] | None = None,
) -> StructuredResult:
    """도구 없이 claude를 한 번 실행하고 스키마에 맞춘 JSON 객체를 받는다 (8.4).

    파이프라인의 지시는 system_prompt(--append-system-prompt)로만, 믿지 않는 자료는 prompt(표준
    입력)로만 넘긴다. cwd는 이 호출만의 빈 임시 폴더라 저장소의 파일·설정이 보이지 않는다.
    """
    if not system_prompt or system_prompt.startswith("-"):
        raise ValueError("시스템 프롬프트가 비었거나 '-'로 시작한다")
    extra = ("--append-system-prompt", system_prompt, "--json-schema", json.dumps(schema))
    args = build_args("no_tools", model, max_budget_usd, extra)
    try:
        with tempfile.TemporaryDirectory(prefix="claude-no-tools-") as empty:
            proc = runner(args, prompt, Path(empty), claude_env(environ), timeout)
    except subprocess.TimeoutExpired:
        return _structured_failure(AIErrorKind.TIMEOUT, f"{timeout:g}초 안에 끝나지 않음")
    except OSError as e:
        return _structured_failure(AIErrorKind.LAUNCH, str(e))
    return parse_structured_output(proc.stdout, proc.returncode, proc.stderr)


def parse_output(stdout: str, returncode: int, stderr: str = "") -> ClaudeResult:
    """`--output-format json` 출력을 읽는다. 실패해도 JSON에 있는 비용과 거부 목록은 남긴다."""
    data = _load(stdout)
    if data is None:
        return _failure(AIErrorKind.BAD_JSON, _tail(stderr or stdout), returncode=returncode)
    error, detail = _status(data, returncode)
    turns = data.get("num_turns")
    text = data.get("result")
    return ClaudeResult(
        ok=error is None,
        error=error,
        cost_usd=_cost(data),
        num_turns=turns if isinstance(turns, int) else 0,
        permission_denials=_denials(data.get("permission_denials")),
        text=text if isinstance(text, str) else "",
        detail=detail,
    )


def parse_structured_output(stdout: str, returncode: int, stderr: str = "") -> StructuredResult:
    """`--json-schema`를 준 호출의 출력. `structured_output`, 없으면 `result`를 JSON으로 읽는다."""
    data = _load(stdout)
    if data is None:
        detail = f"종료 코드 {returncode}: {_tail(stderr or stdout)}"
        return _structured_failure(AIErrorKind.BAD_JSON, detail)
    error, detail = _status(data, returncode)
    answer = None
    if error is None:
        answer = _answer(data)
        if answer is None:
            error, detail = AIErrorKind.BAD_JSON, "구조화 출력이 없거나 JSON 객체가 아님"
    input_tokens, output_tokens = _usage(data.get("usage"))
    return StructuredResult(
        ok=error is None,
        error=error,
        data=answer,
        model_id=_model_id(data.get("modelUsage")),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=_cost(data),
        permission_denials=len(_denials(data.get("permission_denials"))),
        detail=detail,
    )


def _load(stdout: str) -> dict | None:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _status(data: dict, returncode: int) -> tuple[AIErrorKind | None, str]:
    """종료 코드와 is_error를 같이 본다. 없는 모델 이름도 subtype은 "success"로 끝난다."""
    subtype = str(data.get("subtype", ""))
    if "budget" in subtype:
        error = AIErrorKind.BUDGET
    elif returncode != 0 or data.get("is_error") is True:
        error = AIErrorKind.EXIT
    else:
        return None, ""
    return error, f"종료 코드 {returncode}, subtype {subtype}"


def _cost(data: dict) -> float | None:
    cost = data.get("total_cost_usd")
    return float(cost) if isinstance(cost, int | float) else None


def _answer(data: dict) -> dict | None:
    structured = data.get("structured_output")
    if isinstance(structured, dict):
        return structured
    text = data.get("result")
    return _load(text) if isinstance(text, str) else None


def _usage(raw: object) -> tuple[int | None, int | None]:
    if not isinstance(raw, dict):
        return None, None
    parts = [raw.get(k) for k in _INPUT_TOKEN_KEYS]
    numbers = [p for p in parts if isinstance(p, int)]
    output = raw.get("output_tokens")
    return (sum(numbers) if numbers else None), (output if isinstance(output, int) else None)


def _model_id(raw: object) -> str:
    """`modelUsage`의 키 중 출력이 가장 많은 모델. 같으면 이름순으로 앞의 것."""
    if not isinstance(raw, dict) or not raw:
        return ""

    def output(name: str) -> int:
        entry = raw[name]
        tokens = entry.get("outputTokens") if isinstance(entry, dict) else None
        return tokens if isinstance(tokens, int) else 0

    return min(raw, key=lambda name: (-output(name), name))


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


def _structured_failure(kind: AIErrorKind, detail: str) -> StructuredResult:
    return StructuredResult(
        ok=False, error=kind, data=None, model_id="", input_tokens=None, output_tokens=None,
        cost_usd=None, permission_denials=0, detail=detail,
    )  # fmt: skip


def _tail(text: str, limit: int = 500) -> str:
    return text.strip()[-limit:]
