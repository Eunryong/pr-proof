"""로컬 OpenAI 호환 `/v1/chat/completions` 요청·응답과 도구 스키마 계산 (외부 호출 없음).

Ollama, llama.cpp(`llama-server`), LM Studio, vLLM 등 OpenAI 호환 로컬 서버에 넘기는 요청 본문과
응답 파싱, 도구 정의, `<think>` 제거 및 JSON 추출을 담당한다.
"""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from app.worker.model import mcp_pure

READ_FILE: Final = "read_file"
GREP: Final = "grep"
GLOB: Final = "glob"
WRITE_FILE: Final = "write_file"
EDIT_FILE: Final = "edit_file"

READ_TOOL_NAMES: Final[frozenset[str]] = frozenset({READ_FILE, GREP, GLOB})
WRITE_TOOL_NAMES: Final[frozenset[str]] = frozenset({WRITE_FILE, EDIT_FILE})
FILE_TOOL_NAMES: Final[frozenset[str]] = READ_TOOL_NAMES | WRITE_TOOL_NAMES

MAX_TOOL_RESULT_CHARS: Final = 16000
_THINK_RE: Final = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE: Final = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def _fn(name: str, description: str, parameters: Mapping[str, object]) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": dict(parameters),
        },
    }


READ_TOOL_SCHEMAS: Final[tuple[dict[str, object], ...]] = (
    _fn(
        READ_FILE,
        "작업 폴더 안의 일반 파일을 줄 번호와 함께 읽는다. 폴더 밖 경로나 심볼릭 링크는 거부된다.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "작업 폴더 기준 상대 경로"},
                "offset": {
                    "type": "integer",
                    "description": "읽기 시작할 1-based 줄 번호 (기본 1)",
                    "minimum": 1,
                },
                "limit": {
                    "type": "integer",
                    "description": "최대 줄 수 (기본 200)",
                    "minimum": 1,
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    ),
    _fn(
        GREP,
        "작업 폴더 안의 일반 파일에서 정규표현식 패턴을 찾는다.",
        {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "검색할 정규표현식"},
                "path": {
                    "type": "string",
                    "description": "검색할 하위 폴더나 파일 경로 (생략하면 작업 폴더 전체)",
                },
                "glob": {
                    "type": "string",
                    "description": "대상 파일 이름 glob 필터 (예: *.py)",
                },
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    ),
    _fn(
        GLOB,
        "작업 폴더 안에서 glob 패턴과 맞는 일반 파일 경로 목록을 찾는다.",
        {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "glob 패턴 (예: **/*.py, src/*.py)"},
                "path": {
                    "type": "string",
                    "description": "검색 기준 하위 폴더 (생략하면 작업 폴더 전체)",
                },
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    ),
)

WRITE_TOOL_SCHEMAS: Final[tuple[dict[str, object], ...]] = (
    _fn(
        WRITE_FILE,
        "작업 폴더 안의 파일을 새로 쓰거나 덮어쓴다. 폴더 밖 경로나 심볼릭 링크는 거부된다.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "작업 폴더 기준 상대 경로"},
                "content": {"type": "string", "description": "파일에 쓸 전체 UTF-8 내용"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    ),
    _fn(
        EDIT_FILE,
        "작업 폴더 안의 파일에서 정확한 문자열 old_string을 new_string으로 바꾼다.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "작업 폴더 기준 상대 경로"},
                "old_string": {"type": "string", "description": "바꿀 기존 문자열"},
                "new_string": {"type": "string", "description": "대체할 새 문자열"},
                "replace_all": {
                    "type": "boolean",
                    "description": "true이면 모든 일치를 바꾼다 (기본 false: 한 곳만 일치해야 함)",
                },
            },
            "required": ["path", "old_string", "new_string"],
            "additionalProperties": False,
        },
    ),
)


def pipeline_tool_schemas(names: Sequence[str]) -> list[dict[str, object]]:
    """`mcp_pure.TOOLS` 중 요청된 파이프라인 도구를 OpenAI 함수 도구 꼴로 바꾼다."""
    wanted = set(names)
    result: list[dict[str, object]] = []
    for spec in mcp_pure.TOOLS:
        name = str(spec["name"])
        if name in wanted:
            result.append(_fn(name, str(spec["description"]), spec["inputSchema"]))
    return result


def reading_tools() -> list[dict[str, object]]:
    return list(READ_TOOL_SCHEMAS)


def fix_tools(pipeline_names: Sequence[str] = ()) -> list[dict[str, object]]:
    return [*READ_TOOL_SCHEMAS, *WRITE_TOOL_SCHEMAS, *pipeline_tool_schemas(pipeline_names)]


def build_chat_request(
    *,
    model: str,
    messages: Sequence[Mapping[str, object]],
    tools: Sequence[Mapping[str, object]] | None = None,
    json_schema: Mapping[str, object] | None = None,
    json_object: bool = False,
) -> dict[str, object]:
    """OpenAI `/v1/chat/completions` 요청 본문."""
    payload: dict[str, object] = {
        "model": model,
        "messages": [dict(m) for m in messages],
        "stream": False,
    }
    if tools:
        payload["tools"] = [dict(t) for t in tools]
        payload["tool_choice"] = "auto"
    if json_schema is not None:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "response",
                "strict": True,
                "schema": dict(json_schema),
            },
        }
    elif json_object:
        payload["response_format"] = {"type": "json_object"}
    return payload


def schema_fallback_system(system_prompt: str, schema: Mapping[str, object]) -> str:
    """`response_format.json_schema`를 받지 않는 서버용 시스템 프롬프트 보강."""
    rendered = json.dumps(schema, ensure_ascii=False)
    return (
        f"{system_prompt}\n\n"
        "반드시 아래 JSON Schema에 맞는 단일 JSON 객체만 출력하라. "
        "다른 설명이나 마크다운을 붙이지 마라.\n"
        f"{rendered}"
    )


# 읽기를 마친 뒤 최종 답만 따로 묻는 마지막 턴의 사용자 메시지
FINAL_ANSWER_PROMPT: Final = (
    "읽기를 마쳤다. 지금까지 읽은 내용으로 최종 답을 "
    "주어진 JSON 스키마에 맞는 JSON 객체 하나로만 답하라."
)


def reading_system_prompt(system_prompt: str) -> str:
    """`reading` 호출에서 읽기 도구의 사용을 안내하는 시스템 프롬프트. 최종 답은 읽기가 끝난 뒤
    스키마를 강제한 턴에서 따로 받는다."""
    return (
        f"{system_prompt}\n\n"
        "필요한 파일은 `read_file`, `grep`, `glob` 도구로만 읽는다. 더 읽을 것이 없으면 도구를 "
        '부르지 말고 "읽기 끝"이라고만 답한다. 최종 답은 그다음에 따로 묻는다.'
    )


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments_raw: str


@dataclass(frozen=True)
class ChatResponse:
    content: str
    tool_calls: tuple[ToolCall, ...]
    finish_reason: str
    model_id: str
    input_tokens: int | None
    output_tokens: int | None


def parse_chat_response(data: object) -> ChatResponse | None:
    """`/v1/chat/completions` 응답 JSON을 읽는다. 형식이 깨졌으면 None."""
    if not isinstance(data, Mapping):
        return None
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        return None
    first = choices[0]
    message = first.get("message")
    if not isinstance(message, Mapping):
        return None
    raw_content = message.get("content")
    content = raw_content if isinstance(raw_content, str) else ""
    calls: list[ToolCall] = []
    raw_calls = message.get("tool_calls")
    if isinstance(raw_calls, list):
        for idx, item in enumerate(raw_calls):
            if not isinstance(item, Mapping):
                continue
            fn = item.get("function")
            if not isinstance(fn, Mapping):
                continue
            name = fn.get("name")
            if not isinstance(name, str) or not name:
                continue
            raw_args = fn.get("arguments")
            if isinstance(raw_args, Mapping):
                args_str = json.dumps(raw_args, ensure_ascii=False)
            elif isinstance(raw_args, str):
                args_str = raw_args
            else:
                args_str = "{}"
            call_id = item.get("id")
            cid = call_id if isinstance(call_id, str) and call_id else f"call_{idx}"
            calls.append(ToolCall(id=cid, name=name, arguments_raw=args_str))
    usage = data.get("usage")
    in_tok = _int_or_none(usage.get("prompt_tokens")) if isinstance(usage, Mapping) else None
    out_tok = _int_or_none(usage.get("completion_tokens")) if isinstance(usage, Mapping) else None
    model_id = data.get("model")
    finish = first.get("finish_reason")
    return ChatResponse(
        content=content,
        tool_calls=tuple(calls),
        finish_reason=finish if isinstance(finish, str) else "",
        model_id=model_id if isinstance(model_id, str) else "",
        input_tokens=in_tok,
        output_tokens=out_tok,
    )


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def parse_tool_arguments(raw: str) -> dict[str, object] | None:
    """도구 호출의 JSON 문자열 인자를 dict로 읽는다."""
    text = raw.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def extract_json_object(text: str) -> dict[str, object] | None:
    """모델 출력 글에서 JSON 객체 하나를 꺼낸다. `<think>` 블록과 마크다운 코드 울타리를 벗긴다."""
    cleaned = _THINK_RE.sub("", text).strip()
    if not cleaned:
        return None
    for candidate in _json_candidates(cleaned):
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _json_candidates(text: str) -> list[str]:
    candidates = [text]
    for match in _FENCE_RE.finditer(text):
        inner = match.group(1).strip()
        if inner:
            candidates.append(inner)
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    return candidates


def assistant_message(response: ChatResponse) -> dict[str, object]:
    """대화 기록에 쌓을 assistant 메시지."""
    msg: dict[str, object] = {"role": "assistant", "content": response.content}
    if response.tool_calls:
        msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": tc.arguments_raw},
            }
            for tc in response.tool_calls
        ]
    return msg


def tool_message(
    call_id: str, text: str, *, max_chars: int = MAX_TOOL_RESULT_CHARS
) -> dict[str, object]:
    """대화 기록에 쌓을 tool 결과 메시지."""
    return {"role": "tool", "tool_call_id": call_id, "content": clip_result(text, max_chars)}


def clip_result(text: str, max_chars: int = MAX_TOOL_RESULT_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n… (잘림)"
