"""`app/worker/model/local/openai_pure.py`와 로컬 URL·모델 이름 규칙 단위 테스트."""

import pytest

from app.shared.model_names_pure import is_local_openai_url
from app.worker.model.local import openai_pure


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/v1",
        "http://localhost:11434/v1",
        "http://[::1]:8080/v1",
        "http://localhost",
        "http://127.0.0.1:1234/v1/sub",
    ],
)
def test_local_openai_url_accepts_loopback_http(url: str) -> None:
    assert is_local_openai_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8080/v1",
        "http://api.openai.com/v1",
        "http://192.168.0.10:8080/v1",
        "http://localhost.evil.com/v1",
        "http://user:pass@localhost:8080/v1",
        "http://localhost:0/v1",
        "http://localhost:70000/v1",
        "http://localhost:8080/v1?x=1",
        "http://localhost:8080/v1#frag",
        "",
    ],
)
def test_local_openai_url_rejects_non_local_or_bad_urls(url: str) -> None:
    assert is_local_openai_url(url) is False


def test_extract_json_object_handles_think_and_fences() -> None:
    raw = '<think>생각 중...\n{"ignored": true}</think>\n```json\n{"ok": 1, "items": []}\n```'
    assert openai_pure.extract_json_object(raw) == {"ok": 1, "items": []}
    assert openai_pure.extract_json_object('{"a": "b"}') == {"a": "b"}
    assert openai_pure.extract_json_object('앞말 {"x": 2} 뒷말') == {"x": 2}
    assert openai_pure.extract_json_object("[1, 2, 3]") is None
    assert openai_pure.extract_json_object("not json") is None


def test_parse_chat_response_reads_content_tool_calls_and_usage() -> None:
    data = {
        "model": "inkling-2-mini",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                        },
                        {
                            "id": "call_2",
                            "type": "function",
                            "function": {"name": "check_rules", "arguments": {}},
                        },
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 45},
    }
    parsed = openai_pure.parse_chat_response(data)
    assert parsed is not None
    assert parsed.model_id == "inkling-2-mini"
    assert parsed.content == ""
    assert (parsed.input_tokens, parsed.output_tokens) == (120, 45)
    assert len(parsed.tool_calls) == 2
    assert parsed.tool_calls[0].name == "read_file"
    assert openai_pure.parse_tool_arguments(parsed.tool_calls[0].arguments_raw) == {"path": "a.py"}
    assert openai_pure.parse_tool_arguments(parsed.tool_calls[1].arguments_raw) == {}


def test_parse_chat_response_rejects_malformed_data() -> None:
    assert openai_pure.parse_chat_response({}) is None
    assert openai_pure.parse_chat_response({"choices": []}) is None
    assert openai_pure.parse_chat_response("bad") is None


def test_build_chat_request_variants() -> None:
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    req = openai_pure.build_chat_request(
        model="inkling-2-mini",
        messages=[{"role": "user", "content": "hi"}],
        json_schema=schema,
    )
    assert req["response_format"]["type"] == "json_schema"
    req_obj = openai_pure.build_chat_request(
        model="inkling-2-mini",
        messages=[{"role": "user", "content": "hi"}],
        json_object=True,
    )
    assert req_obj["response_format"] == {"type": "json_object"}


def test_clip_result_truncates_long_text() -> None:
    assert openai_pure.clip_result("abc", max_chars=10) == "abc"
    clipped = openai_pure.clip_result("a" * 20, max_chars=5)
    assert clipped.startswith("aaaaa") and "잘림" in clipped


def test_reading_turns_get_read_tools_only_and_the_final_turn_forces_the_schema() -> None:
    """읽는 턴에는 읽기 도구만 주고, 최종 답은 도구 없이 스키마를 강제한 턴에서 받는다."""
    assert [t["function"]["name"] for t in openai_pure.reading_tools()] == [
        "read_file",
        "grep",
        "glob",
    ]
    assert "읽기 끝" in openai_pure.reading_system_prompt(
        "지시"
    ) and "지시" in openai_pure.reading_system_prompt("지시")
    history = [{"role": "system", "content": "s"}, {"role": "assistant", "content": "읽기 끝"}]
    asked = openai_pure.final_answer_messages(history)
    assert asked[:-1] == history and asked[-1] == {
        "role": "user",
        "content": openai_pure.FINAL_ANSWER_PROMPT,
    }
    schema = {"type": "object", "properties": {"seen": {"type": "string"}}}
    request = openai_pure.build_chat_request(model="m", messages=asked, json_schema=schema)
    assert "tools" not in request
    assert request["response_format"]["json_schema"]["schema"] == schema
