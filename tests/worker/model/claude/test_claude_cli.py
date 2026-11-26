import json
from typing import Final

from app.worker.model.claude import claude_cli as ai
from app.worker.model.claude.claude_cli import AIErrorKind

FILE_TOOLS: Final = ["Edit", "Glob", "Grep", "Read", "Write"]


def _stream(result: dict, tools: list[str] | None) -> str:
    """stream-json 출력: system/init 한 줄(tools가 None이면 없음)과 result 한 줄."""
    init = [] if tools is None else [{"type": "system", "subtype": "init", "tools": tools}]
    return "".join(json.dumps(e) + "\n" for e in [*init, {"type": "assistant"}, result])


def _output(tools: list[str] | None = FILE_TOOLS, **fields: object) -> str:
    data = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "total_cost_usd": 0.0017,
        "num_turns": 3,
        "result": "고쳤습니다",
        "permission_denials": [],
    }
    data.update(fields)
    return _stream(data, tools)


def test_claude_env_passes_config_dir_when_set() -> None:
    assert ai.claude_env({"CLAUDE_CONFIG_DIR": "/c", "LANG": "C"}) == {"CLAUDE_CONFIG_DIR": "/c"}


def test_missing_fields_are_defaulted() -> None:
    result = ai.parse_output(_stream({"type": "result"}, FILE_TOOLS), 0)
    assert result.ok
    assert (result.cost_usd, result.num_turns, result.text) == (None, 0, "")
    assert result.permission_denials == ()


def test_malformed_denials_are_skipped() -> None:
    raw = _output(permission_denials=["x", {"tool_name": "Write", "tool_input": "bad"}])
    result = ai.parse_output(raw, 0)
    assert result.permission_denials == (ai.PermissionDenial("Write", {}),)


def _structured_output(tools: list[str] | None = None, **fields: object) -> str:
    """2.1.327에서 --json-schema를 준 호출의 실제 출력 모양 (필요한 키만)."""
    data = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 2,
        "result": '{"rules": []}',
        "structured_output": {"rules": []},
        "total_cost_usd": 0.0008,
        "usage": {
            "input_tokens": 2,
            "cache_creation_input_tokens": 3672,
            "cache_read_input_tokens": 100,
            "output_tokens": 40,
        },
        "modelUsage": {"claude-haiku-5-5": {"inputTokens": 2, "outputTokens": 40}},
        "permission_denials": [],
    }
    data.update(fields)
    return _stream(data, ["StructuredOutput"] if tools is None else tools)


# --- 도구 목록 확인 ---


def test_a_successful_call_without_init_is_an_error() -> None:
    result = ai.parse_output(_output(tools=None), 0)
    assert not result.ok and result.error == AIErrorKind.TOOLS
    structured = ai.parse_structured_output(_structured_output(tools=None).split("\n", 1)[1], 0)
    assert structured.error == AIErrorKind.TOOLS and structured.data is None


def test_a_failed_call_without_init_keeps_its_own_error() -> None:
    result = ai.parse_output(_output(tools=None, is_error=True, result="Not logged in"), 1)
    assert result.error == AIErrorKind.EXIT


def test_wrong_tools_win_over_other_errors_in_finished_output() -> None:
    result = ai.parse_output(_output(tools=[*FILE_TOOLS, "Bash"], is_error=True), 1)
    assert result.error == AIErrorKind.TOOLS


def _tool_use(*names: str) -> dict:
    content = [{"type": "tool_use", "name": n, "input": {}} for n in names]
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}, *content]}}


def test_tool_use_outside_the_list_is_counted_by_name() -> None:
    init = {"type": "system", "subtype": "init", "tools": FILE_TOOLS}
    result = {"type": "result", "subtype": "success", "is_error": False}
    events = [init, _tool_use("Read", "Bash"), _tool_use("Edit"), _tool_use("Bash", "WebSearch")]
    stdout = "".join(json.dumps(e) + "\n" for e in [*events, result])
    parsed = ai.parse_output(stdout, 0)
    assert parsed.ok  # 없는 도구를 부른 시도는 CLI가 거절한다. 호출은 실패가 아니다
    assert parsed.unexpected_tools == (("Bash", 2), ("WebSearch", 1))
    assert ai.parse_output(_output(), 0).unexpected_tools == ()
