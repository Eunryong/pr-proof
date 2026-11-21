"""수정 작성의 모델이 부르는 파이프라인 도구의 MCP 메시지 (docs/DESIGN.md 4.7, 8.4).

claude CLI가 띄우는 stdio MCP 서버(`mcp_relay.py`)는 줄을 워커로 넘기기만 하고, 메시지의 뜻은 여기서
정한다. 도구는 둘이고 인자는 없거나 좁다. 도구를 실제로 돌리는 함수는 부르는 쪽이 넘긴다
(`app/worker/ai/tool_server.py`). 외부 호출이 없다. 수정 작성(`app/worker/fix/`)과 claude 호출
(`app/worker/ai/`)이 같이 쓴다.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

SERVER: Final = "proof"
CHECK_RULES: Final = "check_rules"
RUN_TESTS: Final = "run_tests"
# claude가 MCP 도구에 붙이는 이름. 도구 목록 확인(T25)과 경로 감시 훅이 이 이름을 안다
TOOL_NAMES: Final = (f"mcp__{SERVER}__{CHECK_RULES}", f"mcp__{SERVER}__{RUN_TESTS}")
STATE_TYPES: Final = (("app.worker.mcp_pure", "ToolUsage"),)
# 호출 하나(claude 한 번) 안의 상한 (9.2). 넘으면 도구를 돌리지 않고 "상한에 닿음"을 돌려준다
MAX_TOOL_CALLS: Final = 24
MAX_TOOL_SECONDS: Final = 900.0
MAX_RESULT_CHARS: Final = 8000
LIMIT_REACHED: Final = "상한에 닿음"
_PROTOCOL: Final = "2025-06-18"

TOOLS: Final = (
    {
        "name": CHECK_RULES,
        "description": (
            "작업 폴더의 지금 파일 상태로 대상 규칙의 검사(저장소의 도구와 저장된 검사기)를 돌려, "
            "남은 대상 위반과 새로 생긴 위반을 돌려준다. 인자가 없다."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": RUN_TESTS,
        "description": (
            "샌드박스에서 pytest를 돌린다. code가 before면 PR의 원래 코드에 네가 만든 테스트 "
            "파일만 얹어 돌리고, 만든 테스트마다 통과 여부, 실행한 대상 위반, 출구 검사가 "
            "받아들일지를 돌려준다. now면 지금 파일 상태로 모든 테스트를 돌린다."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"code": {"type": "string", "enum": ["now", "before"]}},
            "additionalProperties": False,
        },
    },
)


@dataclass(frozen=True)
class ToolLimits:
    calls: int = MAX_TOOL_CALLS
    seconds: float = MAX_TOOL_SECONDS


@dataclass(frozen=True)
class ToolUsage:
    """수정 작성 호출들 안의 도구 호출 (9.4). limited는 상한에 닿아 돌리지 않은 호출이 있었다."""

    check_rules: int = 0
    run_tests: int = 0
    seconds: float = 0.0
    limited: bool = False

    @property
    def calls(self) -> int:
        return self.check_rules + self.run_tests

    def __add__(self, other: "ToolUsage") -> "ToolUsage":
        return ToolUsage(
            self.check_rules + other.check_rules, self.run_tests + other.run_tests,
            self.seconds + other.seconds, self.limited or other.limited,
        )  # fmt: skip


def limit_reason(calls: int, seconds: float, limits: ToolLimits) -> str | None:
    """지금까지 돌린 도구 호출 수와 시간으로 다음 호출을 돌리지 않을 이유."""
    if calls >= limits.calls:
        return f"{LIMIT_REACHED}: 도구 호출 {limits.calls}회. 더 부르지 말고 마무리한다"
    if seconds >= limits.seconds:
        return f"{LIMIT_REACHED}: 도구 시간 {limits.seconds:g}초. 더 부르지 말고 마무리한다"
    return None


def tool_arguments(name: str, raw: object) -> dict[str, str] | None:
    """도구 인자를 좁힌다. 모르는 도구나 허용하지 않는 인자는 None."""
    args = raw if isinstance(raw, dict) else {} if raw is None else None
    if args is None:
        return None
    if name == CHECK_RULES:
        return {} if not args else None
    if name == RUN_TESTS:
        code = args.get("code", "now")
        return {"code": code} if set(args) <= {"code"} and code in ("now", "before") else None
    return None


def _result(message_id: object, result: Mapping[str, object]) -> dict:
    return {"jsonrpc": "2.0", "id": message_id, "result": dict(result)}


def _error(message_id: object, code: int, text: str) -> dict:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": text}}


def _text(text: str, *, error: bool = False) -> dict:
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + "\n… (잘림)"
    return {"content": [{"type": "text", "text": text}], "isError": error}


# (도구 이름, 좁힌 인자) → (결과 글, 오류인지)
Call = Callable[[str, Mapping[str, str]], tuple[str, bool]]


def respond(line: str, call: Call) -> dict | None:
    """요청 한 줄에 대한 응답. 알림(id 없음)과 읽지 못한 줄에는 답하지 않는다."""
    try:
        message = json.loads(line)
    except ValueError:
        return None
    if not isinstance(message, dict) or "id" not in message or "method" not in message:
        return None
    mid, method, params = message["id"], message["method"], message.get("params") or {}
    if method == "initialize":
        version = params.get("protocolVersion") if isinstance(params, dict) else None
        return _result(mid, {
            "protocolVersion": version if isinstance(version, str) else _PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER, "version": "1"},
        })  # fmt: skip
    if method == "ping":
        return _result(mid, {})
    if method == "tools/list":
        return _result(mid, {"tools": list(TOOLS)})
    if method == "tools/call" and isinstance(params, dict):
        name = params.get("name")
        args = tool_arguments(name, params.get("arguments")) if isinstance(name, str) else None
        if args is None:
            return _result(mid, _text(f"알 수 없는 도구나 인자: {name!r}", error=True))
        text, failed = call(name, args)
        return _result(mid, _text(text, error=failed))
    return _error(mid, -32601, f"지원하지 않는 메서드: {method}")


def mcp_config(command: str, args: list[str]) -> str:
    """`--mcp-config`에 넘기는 JSON. 서버는 파이프라인이 띄우는 것 하나뿐이다."""
    return json.dumps({"mcpServers": {SERVER: {"type": "stdio", "command": command, "args": args}}})
