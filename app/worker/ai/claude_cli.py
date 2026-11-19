"""claude CLI 호출 (docs/DESIGN.md 8.4).

호출 모양은 둘이다. 수정 작성은 worktree를 cwd로 파일 도구 다섯 개만 주고(`run_claude`), 나머지
단계는 빈 임시 폴더를 cwd로 도구 없이 부르고 구조화 출력을 받는다(`run_structured`). 둘 다
권한을 제한한 같은 옵션 위에 모양별 옵션을 더한다. 모델과 호출당 예산은 호출하는 쪽이 정한다.
"""

import json
import logging
import os
import subprocess
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, Literal

# 호출 한 번의 시간 상한(초). 넘으면 프로세스를 죽이고 TIMEOUT으로 돌려준다
DEFAULT_TIMEOUT_S: Final = 600.0
# --max-budget-usd에 소수 넷째 자리까지 넘기므로 이보다 작으면 0이 된다
MIN_BUDGET_USD: Final = 0.0001

NOT_LOGGED_IN: Final = "Not logged in"

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
    "stream-json",  # 첫 줄의 system/init에 도구 목록이 있다. json 형식에는 없다
    "--verbose",
)
FILE_TOOLS: Final = "Read,Grep,Glob,Write,Edit"
CallShape = Literal["file_tools", "no_tools"]
_SHAPE_ARGS: Final[Mapping[str, tuple[str, ...]]] = {
    "file_tools": ("--tools", FILE_TOOLS, "--allowedTools", FILE_TOOLS),
    "no_tools": ("--tools", ""),
}
# --json-schema를 주면 CLI가 답을 받는 도구 하나를 스스로 더한다. 파일·웹·실행 도구가 아니다
STRUCTURED_OUTPUT_TOOL: Final = "StructuredOutput"
# 모양마다 system/init의 도구 목록이 정확히 이것이어야 한다. 다르면 호출을 중단한다
_SHAPE_TOOLS: Final[Mapping[str, frozenset[str]]] = {
    "file_tools": frozenset(FILE_TOOLS.split(",")),
    "no_tools": frozenset({STRUCTURED_OUTPUT_TOOL}),
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


log = logging.getLogger(__name__)


class AIErrorKind(StrEnum):
    TIMEOUT = "timeout"
    LAUNCH = "launch"  # claude를 실행하지 못함 (명령 없음, worktree 없음)
    EXIT = "exit"  # 0이 아닌 종료 코드나 is_error (로그인 풀림 포함)
    BAD_JSON = "bad_json"
    BUDGET = "budget"  # --max-budget-usd를 넘김
    # 모델에 보인 도구 목록이 기대와 다르거나, 경로 감시 훅이 돌지 않음. 결과를 쓰지 않는다
    TOOLS = "tools"
    APPLY = "apply"  # 모델 폴더의 변경을 worktree에 적용하지 못함


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
    # 모델이 부르려 한, 허용 목록 밖 도구의 (이름, 횟수). 목록에 없는 도구는 CLI가 "없는 도구"로
    # 답해 permission_denials에 잡히지 않으므로 스트림의 tool_use에서 센다
    unexpected_tools: tuple[tuple[str, int], ...] = ()
    # 모델이 부른, 목록 안 도구의 횟수. 경로 감시 훅이 호출마다 돌았는지 맞춰 보는 데 쓴다
    tool_uses: int = 0
    model_id: str = ""  # 별칭이 가리킨 전체 모델 id. 모르면 ""
    input_tokens: int | None = None  # 캐시 생성·읽기를 합친 실제 입력. 모르면 None
    output_tokens: int | None = None
    # 모델 폴더에서 worktree에 적용한 diff. `workdir.run_in_model_folder`만 채운다
    applied: bytes = b""


class AIEnvironmentError(RuntimeError):
    """claude를 실행할 수 없다 (명령 없음, 로그인 풀림). PR 탓이 아니라 환경 오류로 재시도한다."""


def environment_problem(result: ClaudeResult) -> str | None:
    """PR과 관계없이 claude를 쓸 수 없는 상태면 그 이유."""
    if result.error == AIErrorKind.LAUNCH:
        return f"claude를 실행하지 못함: {result.detail}"
    if result.error == AIErrorKind.TOOLS:
        return f"claude 호출을 중단함: {result.detail}"
    if NOT_LOGGED_IN in result.text or NOT_LOGGED_IN in result.detail:
        return "claude 로그인이 풀림"
    return None


# 표준 출력 한 줄 → 호출을 중단할 이유. 계속해도 되면 None
Watch = Callable[[str], str | None]
# (args, 표준 입력, cwd, env, timeout, watch) → 완료된 프로세스. 시간 초과는 TimeoutExpired로,
# watch가 이유를 내면 프로세스를 죽이고 ToolListMismatch로 올린다
Runner = Callable[
    [list[str], str, Path, dict[str, str], float, Watch], subprocess.CompletedProcess[str]
]


class ToolListMismatch(Exception):
    """system/init의 도구 목록이 기대와 달라 호출을 중단했다."""


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


def tool_list_problem(event: Mapping[str, object], shape: CallShape) -> str | None:
    """system/init 메시지의 도구 목록이 이 모양의 기대와 정확히 같지 않으면 그 이유."""
    tools = event.get("tools")
    if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
        return "system/init에 도구 목록이 없음"
    expected, seen = _SHAPE_TOOLS[shape], set(tools)
    if seen == expected:
        return None
    extra, missing = sorted(seen - expected), sorted(expected - seen)
    version = event.get("claude_code_version")
    return (
        f"도구 목록이 기대와 다름: 더 있음 {extra}, 없음 {missing} "
        f"(claude CLI {version if isinstance(version, str) else '버전 모름'})"
    )


def _init_event(line: str) -> dict | None:
    event = _json_object(line)
    if event is not None and event.get("type") == "system" and event.get("subtype") == "init":
        return event
    return None


def _watch_for(shape: CallShape) -> Watch:
    def watch(line: str) -> str | None:
        event = _init_event(line)
        return None if event is None else tool_list_problem(event, shape)

    return watch


def _run(
    args: list[str], stdin: str, cwd: Path, env: dict[str, str], timeout: float, watch: Watch
) -> subprocess.CompletedProcess[str]:
    """출력을 줄 단위로 읽으며 watch에 보인다. init은 모델을 부르기 전에 나와 거기서 멈춘다."""
    timed_out = threading.Event()
    with (
        tempfile.TemporaryFile("w+") as stderr,
        subprocess.Popen(
            args, cwd=cwd, env=env, text=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
        ) as proc,
    ):  # fmt: skip
        assert proc.stdin is not None and proc.stdout is not None

        def expire() -> None:
            timed_out.set()
            proc.kill()

        def feed() -> None:
            try:
                proc.stdin.write(stdin)
                proc.stdin.close()
            except OSError:  # 프로세스가 입력을 다 읽기 전에 끝남
                pass

        timer = threading.Timer(timeout, expire)
        feeder = threading.Thread(target=feed, daemon=True)
        timer.start()
        feeder.start()
        lines: list[str] = []
        try:
            for line in proc.stdout:
                lines.append(line)
                reason = watch(line)
                if reason is not None:
                    proc.kill()
                    raise ToolListMismatch(reason)
            proc.wait()
        finally:
            timer.cancel()
            proc.kill()
            feeder.join()
        if timed_out.is_set():
            raise subprocess.TimeoutExpired(args, timeout)
        stderr.seek(0)
        return subprocess.CompletedProcess(args, proc.returncode, "".join(lines), stderr.read())


def run_claude(
    worktree: Path,
    prompt: str,
    *,
    model: str,
    max_budget_usd: float,
    timeout: float = DEFAULT_TIMEOUT_S,
    runner: Runner = _run,
    environ: Mapping[str, str] | None = None,
    settings: Path | None = None,
    system_prompt: str | None = None,
) -> ClaudeResult:
    """worktree를 cwd로 파일 도구만 준 claude를 한 번 실행한다. 셸을 거치지 않는다.

    settings는 파이프라인이 쓴 설정 파일(경로 감시 훅)이다. `--restricted`는 사용자·저장소의 설정을
    읽지 않지만 `--settings`로 넘긴 파일은 읽는다 (2.1.328에서 확인).
    system_prompt는 파이프라인의 지시다(`--append-system-prompt`, 8.4). prompt에는 자료만 넣는다.
    """
    extra: tuple[str, ...] = () if settings is None else ("--settings", str(settings))
    if system_prompt is not None:
        _check_system_prompt(system_prompt)
        extra += ("--append-system-prompt", system_prompt)
    args = build_args("file_tools", model, max_budget_usd, extra)
    try:
        watch = _watch_for("file_tools")
        proc = runner(args, prompt, worktree, claude_env(environ), timeout, watch)
    except subprocess.TimeoutExpired:
        return _failure(AIErrorKind.TIMEOUT, f"{timeout:g}초 안에 끝나지 않음")
    except ToolListMismatch as e:
        log.error("claude 수정 호출을 중단함: %s", e)
        return _failure(AIErrorKind.TOOLS, str(e))
    except OSError as e:
        return _failure(AIErrorKind.LAUNCH, str(e))
    result = parse_output(proc.stdout, proc.returncode, proc.stderr)
    if result.error == AIErrorKind.TOOLS:
        log.error("claude 수정 호출의 도구 목록 확인 실패: %s", result.detail)
    return result


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
    _check_system_prompt(system_prompt)
    extra = ("--append-system-prompt", system_prompt, "--json-schema", json.dumps(schema))
    args = build_args("no_tools", model, max_budget_usd, extra)
    try:
        with tempfile.TemporaryDirectory(prefix="claude-no-tools-") as empty:
            watch = _watch_for("no_tools")
            proc = runner(args, prompt, Path(empty), claude_env(environ), timeout, watch)
    except subprocess.TimeoutExpired:
        return _structured_failure(AIErrorKind.TIMEOUT, f"{timeout:g}초 안에 끝나지 않음")
    except ToolListMismatch as e:
        log.error("claude 도구 없는 호출을 중단함: %s", e)
        return _structured_failure(AIErrorKind.TOOLS, str(e))
    except OSError as e:
        return _structured_failure(AIErrorKind.LAUNCH, str(e))
    return parse_structured_output(proc.stdout, proc.returncode, proc.stderr)


def _check_system_prompt(system_prompt: str) -> None:
    if not system_prompt or system_prompt.startswith("-"):
        raise ValueError("시스템 프롬프트가 비었거나 '-'로 시작한다")


def parse_output(stdout: str, returncode: int, stderr: str = "") -> ClaudeResult:
    """stream-json 출력을 읽는다. 실패해도 결과 줄에 있는 비용과 거부 목록은 남긴다."""
    init, data = _events(stdout)
    if data is None:
        return _failure(AIErrorKind.BAD_JSON, _tail(stderr or stdout), returncode=returncode)
    error, detail = _checked_status(init, "file_tools", data, returncode)
    turns = data.get("num_turns")
    text = data.get("result")
    input_tokens, output_tokens = _usage(data.get("usage"))
    return ClaudeResult(
        ok=error is None,
        error=error,
        cost_usd=_cost(data),
        num_turns=turns if isinstance(turns, int) else 0,
        permission_denials=_denials(data.get("permission_denials")),
        text=text if isinstance(text, str) else "",
        detail=detail,
        unexpected_tools=_unexpected_tools(stdout, "file_tools"),
        tool_uses=_expected_tool_uses(stdout, "file_tools"),
        model_id=_model_id(data.get("modelUsage")),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def parse_structured_output(stdout: str, returncode: int, stderr: str = "") -> StructuredResult:
    """`--json-schema`를 준 호출의 출력. `structured_output`, 없으면 `result`를 JSON으로 읽는다."""
    init, data = _events(stdout)
    if data is None:
        detail = f"종료 코드 {returncode}: {_tail(stderr or stdout)}"
        return _structured_failure(AIErrorKind.BAD_JSON, detail)
    error, detail = _checked_status(init, "no_tools", data, returncode)
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


def _json_object(text: str) -> dict | None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _events(stdout: str) -> tuple[dict | None, dict | None]:
    """줄마다 JSON 하나인 출력에서 (system/init, 마지막 result)를 찾는다."""
    init, result = None, None
    for line in stdout.splitlines():
        event = _json_object(line)
        if event is None:
            continue
        if init is None and _init_event(line) is not None:
            init = event
        elif event.get("type") == "result":
            result = event
    return init, result


def _tool_counts(stdout: str) -> dict[str, int]:
    """assistant 메시지의 tool_use를 도구 이름별로 센다."""
    counts: dict[str, int] = {}
    for line in stdout.splitlines():
        event = _json_object(line)
        message = event.get("message") if event and event.get("type") == "assistant" else None
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else ():
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = str(block.get("name", ""))
            counts[name] = counts.get(name, 0) + 1
    return counts


def _unexpected_tools(stdout: str, shape: CallShape) -> tuple[tuple[str, int], ...]:
    """assistant 메시지의 tool_use 중 이 모양의 목록에 없는 이름과 횟수 (이름순)."""
    counts = _tool_counts(stdout)
    return tuple(sorted((n, c) for n, c in counts.items() if n not in _SHAPE_TOOLS[shape]))


def _expected_tool_uses(stdout: str, shape: CallShape) -> int:
    return sum(c for n, c in _tool_counts(stdout).items() if n in _SHAPE_TOOLS[shape])


def _checked_status(
    init: dict | None, shape: CallShape, data: dict, returncode: int
) -> tuple[AIErrorKind | None, str]:
    """호출의 상태에 도구 목록 확인을 더한다. 실행 중 확인(watch)을 거치지 않은 출력도 걸린다.

    init이 없는 실패(로그인 풀림 등)는 원래 오류를 남긴다. 성공인데 init이 없으면 확인하지 못한
    것이므로 오류다.
    """
    error, detail = _status(data, returncode)
    if init is not None:
        problem = tool_list_problem(init, shape)
    else:
        problem = None if error is not None else "system/init이 없어 도구 목록을 확인하지 못함"
    return (AIErrorKind.TOOLS, problem) if problem is not None else (error, detail)


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
    return _json_object(text) if isinstance(text, str) else None


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
