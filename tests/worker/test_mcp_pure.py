"""파이프라인 도구의 MCP 메시지와 상한 (docs/DESIGN.md 4.7, 9.2)."""

import json

import pytest

from app.worker import mcp_pure
from app.worker.mcp_pure import ToolLimits, limit_reason, respond, tool_arguments


def _ask(method: str, params: object = None, mid: int = 1) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})


def _never(name, args):
    raise AssertionError("도구를 돌리면 안 된다")


def test_initialize_echoes_the_protocol_and_offers_tools() -> None:
    reply = respond(_ask("initialize", {"protocolVersion": "2024-11-05"}), _never)
    assert reply["result"]["protocolVersion"] == "2024-11-05"
    assert reply["result"]["capabilities"] == {"tools": {}}


def test_tools_list_has_exactly_the_two_tools() -> None:
    reply = respond(_ask("tools/list"), _never)
    assert [t["name"] for t in reply["result"]["tools"]] == ["check_rules", "run_tests"]
    assert mcp_pure.TOOL_NAMES == ("mcp__proof__check_rules", "mcp__proof__run_tests")


def test_tools_call_runs_the_tool_with_narrowed_arguments() -> None:
    calls = []

    def call(name, args):
        calls.append((name, dict(args)))
        return "결과", False

    reply = respond(_ask("tools/call", {"name": "run_tests", "arguments": {}}), call)
    assert reply["result"] == {"content": [{"type": "text", "text": "결과"}], "isError": False}
    respond(_ask("tools/call", {"name": "check_rules"}), call)
    assert calls == [("run_tests", {"code": "now"}), ("check_rules", {})]


@pytest.mark.parametrize(
    ("name", "args"),
    [("run_tests", {"code": "rm -rf /"}), ("run_tests", {"code": "now", "path": "x"}),
     ("check_rules", {"rule": "R"}), ("Bash", {}), ("run_tests", "now")],
)  # fmt: skip
def test_unknown_tools_and_arguments_are_refused_without_running(name, args) -> None:
    reply = respond(_ask("tools/call", {"name": name, "arguments": args}), _never)
    assert reply["result"]["isError"] is True


def test_notifications_and_garbage_get_no_reply() -> None:
    note = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert respond(note, _never) is None
    assert respond("not json", _never) is None
    assert respond(_ask("resources/list"), _never)["error"]["code"] == -32601


def test_long_tool_text_is_clipped() -> None:
    reply = respond(_ask("tools/call", {"name": "check_rules"}), lambda n, a: ("x" * 9000, False))
    text = reply["result"]["content"][0]["text"]
    assert len(text) < 9000 and text.endswith("(잘림)")


def test_limits_on_calls_and_seconds() -> None:
    limits = ToolLimits(calls=2, seconds=10)
    assert limit_reason(1, 9.9, limits) is None
    assert mcp_pure.LIMIT_REACHED in limit_reason(2, 0, limits)
    assert mcp_pure.LIMIT_REACHED in limit_reason(0, 10, limits)


def test_tool_arguments_default_code_is_now() -> None:
    assert tool_arguments("run_tests", None) == {"code": "now"}
    assert tool_arguments("run_tests", {"code": "before"}) == {"code": "before"}


def test_mcp_config_names_one_stdio_server() -> None:
    config = json.loads(mcp_pure.mcp_config("/py", ["-I", "relay.py", "/s"]))
    assert config == {"mcpServers": {"proof": {"type": "stdio", "command": "/py",
                                               "args": ["-I", "relay.py", "/s"]}}}  # fmt: skip
