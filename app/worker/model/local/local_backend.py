"""로컬 OpenAI 호환 서버(Ollama, llama-server, LM Studio 등)로 하는 모델 구현 (DESIGN 8.4).

`ModelBackend`의 세 호출(`ask`, `reading`, `fix`)과 `startup_check`를 구현한다.
- 로컬 루프백 주소(`127.0.0.1`, `localhost`, `[::1]`)로만 통신한다(API 키 불필요).
- `ask`: 도구 없이 `response_format` JSON Schema로 묻는다. 서버가 `json_schema`를 거부(HTTP
  400/422)하면 `json_object` + 스키마 안내로 한 번 재시도한다.
- `reading`: `workdir.model_folder`가 복사한 일반 파일 전용 폴더에서 파이프라인이 만든 안전한
  읽기 도구(`read_file`, `grep`, `glob`)로 읽게 하고, 읽기가 끝나면 도구 없이 `response_format`
  JSON Schema를 강제한 턴에서 최종 답을 받는다. worktree를 수정하지 않는다.
- `fix`: `workdir.model_folder`에서 안전한 파일 도구(`read_file`, `grep`, `glob`, `write_file`,
  `edit_file`)와 파이프라인 도구(`check_rules`, `run_tests`)로 고치고 diff를 worktree에 적용한다.
"""

import logging
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from app.shared.model_names_pure import (
    DEFAULT_LOCAL_OPENAI_URL,
    LOCAL_OPENAI_BACKEND,
    is_local_openai_url,
)
from app.worker.git import repo
from app.worker.model import mcp_pure
from app.worker.model.claude import workdir
from app.worker.model.local import openai_client, openai_pure
from app.worker.model.local.openai_client import LocalApiError
from app.worker.model.local.sandbox_tools import SandboxTools
from app.worker.model.mcp_pure import ToolLimits, ToolUsage
from app.worker.model.model_backend import (
    AIErrorKind,
    AskModel,
    Capabilities,
    EditResult,
    PipelineTool,
    StartupCheck,
    StructuredResult,
)

NAME: Final = LOCAL_OPENAI_BACKEND
CAPABILITIES: Final = Capabilities(
    reads_code=True,
    reports_reads=True,
    edits=True,
    pipeline_tools=True,
    max_diff_chars=60_000,
    max_checker_attempts=1,
)
SERVER_CHECK: Final = "로컬 모델 서버"
MAX_TIMEOUT_S: Final = 1200.0  # 호출 시간 상한 20분
DEFAULT_MAX_TOOL_USES: Final = 20
MAX_READING_TURNS: Final = 25
MAX_FIX_TURNS: Final = 40

ChatPoster = Callable[..., Mapping[str, object]]
ModelLister = Callable[..., list[str] | None]

log = logging.getLogger(__name__)


@dataclass
class _PipelineCounter:
    limits: ToolLimits = field(default_factory=ToolLimits)
    counts: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0
    limited: bool = False

    def usage(self) -> ToolUsage:
        return ToolUsage(
            check_rules=self.counts.get(mcp_pure.CHECK_RULES, 0),
            run_tests=self.counts.get(mcp_pure.RUN_TESTS, 0),
            seconds=round(self.seconds, 1),
            limited=self.limited,
        )


def _sum_tokens(acc: int | None, value: int | None) -> int | None:
    if value is None:
        return acc
    return value if acc is None else acc + value


def _effective_timeout(timeout: float | None) -> float:
    return MAX_TIMEOUT_S if timeout is None else min(timeout, MAX_TIMEOUT_S)


def _remaining(deadline: float) -> float:
    return max(1.0, deadline - time.monotonic())


class LocalOpenAIBackend:
    """로컬 OpenAI 호환 서버 구현."""

    name: Final = NAME
    capabilities: Capabilities = CAPABILITIES

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_LOCAL_OPENAI_URL,
        model_override: str | None = None,
        post_chat: ChatPoster = openai_client.post_chat,
        list_models: ModelLister = openai_client.list_models,
        tool_limits: ToolLimits | None = None,
    ) -> None:
        cleaned = base_url.rstrip("/")
        if not is_local_openai_url(cleaned):
            raise ValueError(f"로컬 모델 서버 주소가 아님: {base_url}")
        self.base_url = cleaned
        self._model_override = model_override
        self._post_chat = post_chat
        self._list_models = list_models
        self._tool_limits = tool_limits or ToolLimits()

    def _effective_model(self, model: str) -> str:
        return self._model_override or model

    def ask(
        self,
        prompt: str,
        *,
        system_prompt: str,
        schema: Mapping[str, object],
        model: str,
        max_budget_usd: float,
        timeout: float | None = None,
    ) -> StructuredResult:
        del max_budget_usd  # 로컬 서버는 무료이므로 달러 예산을 따로 넘기지 않는다
        messages: list[dict[str, object]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]
        return self._ask_messages(messages, schema, model, _effective_timeout(timeout))

    def _final_answer(
        self,
        messages: list[dict[str, object]],
        schema: Mapping[str, object],
        model: str,
        deadline: float,
    ) -> StructuredResult:
        """읽기가 끝난 대화에 최종 답을 묻는 턴을 더해, 도구 없이 스키마를 강제해 받는다."""
        asked = openai_pure.final_answer_messages(messages)
        return self._ask_messages(asked, schema, model, _remaining(deadline))

    def _ask_messages(
        self,
        messages: list[dict[str, object]],
        schema: Mapping[str, object],
        model: str,
        limit: float,
    ) -> StructuredResult:
        """messages에 `response_format` JSON Schema로 답을 받는다. 서버가 `json_schema`를 거부(HTTP
        400/422)하면 시스템 메시지에 스키마 안내를 붙이고 `json_object`로 한 번 다시 묻는다."""
        model_name = self._effective_model(model)
        payload = openai_pure.build_chat_request(
            model=model_name, messages=messages, json_schema=schema
        )
        try:
            raw = self._post_chat(self.base_url, payload, timeout=limit)
        except LocalApiError as e:
            if e.status in (400, 422):
                system = str(messages[0]["content"])
                fallback_messages = [
                    {
                        "role": "system",
                        "content": openai_pure.schema_fallback_system(system, schema),
                    },
                    *messages[1:],
                ]
                fallback_payload = openai_pure.build_chat_request(
                    model=model_name, messages=fallback_messages, json_object=True
                )
                try:
                    raw = self._post_chat(self.base_url, fallback_payload, timeout=limit)
                except LocalApiError as e2:
                    return _structured_error(e2.kind, e2.detail)
            else:
                return _structured_error(e.kind, e.detail)
        parsed = openai_pure.parse_chat_response(raw)
        if parsed is None:
            return _structured_error(AIErrorKind.BAD_JSON, "로컬 서버 응답 형식을 읽지 못함")
        data = openai_pure.extract_json_object(parsed.content)
        if data is None:
            return StructuredResult(
                ok=False, error=AIErrorKind.BAD_JSON, data=None, model_id=parsed.model_id,
                input_tokens=parsed.input_tokens, output_tokens=parsed.output_tokens,
                cost_usd=0.0, permission_denials=0,
                detail=f"JSON 객체로 읽지 못함: {parsed.content[:200]}", num_turns=1,
            )  # fmt: skip
        return StructuredResult(
            ok=True, error=None, data=data, model_id=parsed.model_id,
            input_tokens=parsed.input_tokens, output_tokens=parsed.output_tokens,
            cost_usd=0.0, permission_denials=0, num_turns=1,
        )  # fmt: skip

    @contextmanager
    def reading(self, worktree: Path) -> Iterator[AskModel]:
        with workdir.model_folder(worktree, repo.list_files(worktree)) as folder:

            def ask_in_folder(
                prompt: str,
                *,
                system_prompt: str,
                schema: Mapping[str, object],
                model: str,
                max_budget_usd: float,
                max_tool_uses: int = DEFAULT_MAX_TOOL_USES,
                timeout: float | None = None,
            ) -> StructuredResult:
                return self._run_reading(
                    folder.work, prompt, system_prompt=system_prompt, schema=schema,
                    model=model, max_budget_usd=max_budget_usd,
                    max_tool_uses=max_tool_uses, timeout=timeout,
                )  # fmt: skip

            yield ask_in_folder

    def _run_reading(
        self,
        work: Path,
        prompt: str,
        *,
        system_prompt: str,
        schema: Mapping[str, object],
        model: str,
        max_budget_usd: float,
        max_tool_uses: int,
        timeout: float | None,
    ) -> StructuredResult:
        del max_budget_usd
        limit = _effective_timeout(timeout)
        deadline = time.monotonic() + limit
        sandbox = SandboxTools(work, allow_write=False)
        tools = openai_pure.reading_tools()
        messages: list[dict[str, object]] = [
            {"role": "system", "content": openai_pure.reading_system_prompt(system_prompt)},
            {"role": "user", "content": prompt},
        ]
        unexpected: dict[str, int] = {}
        in_tokens: int | None = None
        out_tokens: int | None = None
        model_id = ""
        turns = 0

        for _ in range(MAX_READING_TURNS):
            if time.monotonic() >= deadline:
                return _reading_result(
                    ok=False, error=AIErrorKind.TIMEOUT, data=None, model_id=model_id,
                    in_tokens=in_tokens, out_tokens=out_tokens, sandbox=sandbox,
                    unexpected=unexpected, turns=turns, detail="읽기 호출 시간 초과",
                )  # fmt: skip
            payload = openai_pure.build_chat_request(
                model=self._effective_model(model), messages=messages, tools=tools
            )
            try:
                raw = self._post_chat(self.base_url, payload, timeout=_remaining(deadline))
            except LocalApiError as e:
                return _reading_result(
                    ok=False, error=e.kind, data=None, model_id=model_id,
                    in_tokens=in_tokens, out_tokens=out_tokens, sandbox=sandbox,
                    unexpected=unexpected, turns=turns, detail=e.detail,
                )  # fmt: skip
            parsed = openai_pure.parse_chat_response(raw)
            if parsed is None:
                return _reading_result(
                    ok=False, error=AIErrorKind.BAD_JSON, data=None, model_id=model_id,
                    in_tokens=in_tokens, out_tokens=out_tokens, sandbox=sandbox,
                    unexpected=unexpected, turns=turns, detail="로컬 서버 응답 형식을 읽지 못함",
                )  # fmt: skip
            turns += 1
            model_id = parsed.model_id or model_id
            in_tokens = _sum_tokens(in_tokens, parsed.input_tokens)
            out_tokens = _sum_tokens(out_tokens, parsed.output_tokens)
            messages.append(openai_pure.assistant_message(parsed))

            if not parsed.tool_calls:
                final = self._final_answer(messages, schema, model, deadline)
                return _reading_result(
                    ok=final.ok, error=final.error, data=final.data,
                    model_id=final.model_id or model_id,
                    in_tokens=_sum_tokens(in_tokens, final.input_tokens),
                    out_tokens=_sum_tokens(out_tokens, final.output_tokens), sandbox=sandbox,
                    unexpected=unexpected, turns=turns + 1, detail=final.detail,
                )  # fmt: skip

            for tc in parsed.tool_calls:
                args = openai_pure.parse_tool_arguments(tc.arguments_raw)
                if tc.name not in openai_pure.READ_TOOL_NAMES:
                    unexpected[tc.name] = unexpected.get(tc.name, 0) + 1
                    output = sandbox.execute(tc.name, args or {})
                    messages.append(openai_pure.tool_message(tc.id, output))
                    continue
                if sandbox.tool_uses >= max_tool_uses:
                    return _reading_result(
                        ok=False, error=AIErrorKind.TOOL_CAP, data=None, model_id=model_id,
                        in_tokens=in_tokens, out_tokens=out_tokens, sandbox=sandbox,
                        unexpected=unexpected, turns=turns,
                        detail=f"읽기 도구 호출 상한({max_tool_uses}회)을 넘음",
                    )  # fmt: skip
                if args is None:
                    messages.append(
                        openai_pure.tool_message(tc.id, "오류: 도구 인자가 JSON 객체가 아님")
                    )
                    continue
                output = sandbox.execute(tc.name, args)
                messages.append(openai_pure.tool_message(tc.id, output))

        return _reading_result(
            ok=False, error=AIErrorKind.TOOL_CAP, data=None, model_id=model_id,
            in_tokens=in_tokens, out_tokens=out_tokens, sandbox=sandbox,
            unexpected=unexpected, turns=turns, detail=f"읽기 턴 상한({MAX_READING_TURNS})을 넘음",
        )  # fmt: skip

    def fix(
        self,
        worktree: Path,
        prompt: str,
        *,
        system_prompt: str,
        model: str,
        max_budget_usd: float,
        tools: Mapping[str, PipelineTool] | None = None,
        timeout: float | None = None,
    ) -> EditResult:
        del max_budget_usd
        limit = _effective_timeout(timeout)
        deadline = time.monotonic() + limit
        pipeline_names = tuple(tools.keys()) if tools else ()
        tool_schemas = openai_pure.fix_tools(pipeline_names)
        counter = _PipelineCounter(limits=self._tool_limits)

        with workdir.model_folder(worktree, repo.list_files(worktree)) as folder:
            sandbox = SandboxTools(folder.work, allow_write=True)
            messages: list[dict[str, object]] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ]
            unexpected: dict[str, int] = {}
            in_tokens: int | None = None
            out_tokens: int | None = None
            model_id = ""
            last_text = ""
            turns = 0
            error: AIErrorKind | None = None
            detail = ""
            environment = ""

            for _ in range(MAX_FIX_TURNS):
                if time.monotonic() >= deadline:
                    error, detail = AIErrorKind.TIMEOUT, f"수정 호출 시간 초과 ({limit:g}초)"
                    break
                payload = openai_pure.build_chat_request(
                    model=self._effective_model(model), messages=messages, tools=tool_schemas
                )
                try:
                    raw = self._post_chat(self.base_url, payload, timeout=_remaining(deadline))
                except LocalApiError as e:
                    error, detail = e.kind, e.detail
                    if e.environment:
                        environment = e.detail
                    break
                parsed = openai_pure.parse_chat_response(raw)
                if parsed is None:
                    error, detail = AIErrorKind.BAD_JSON, "로컬 서버 응답 형식을 읽지 못함"
                    break
                turns += 1
                model_id = parsed.model_id or model_id
                in_tokens = _sum_tokens(in_tokens, parsed.input_tokens)
                out_tokens = _sum_tokens(out_tokens, parsed.output_tokens)
                if parsed.content:
                    last_text = parsed.content
                messages.append(openai_pure.assistant_message(parsed))
                if not parsed.tool_calls:
                    break

                for tc in parsed.tool_calls:
                    args = openai_pure.parse_tool_arguments(tc.arguments_raw)
                    if tc.name in openai_pure.FILE_TOOL_NAMES:
                        if args is None:
                            messages.append(
                                openai_pure.tool_message(
                                    tc.id, "오류: 도구 인자가 JSON 객체가 아님"
                                )
                            )
                            continue
                        output = sandbox.execute(tc.name, args)
                        messages.append(openai_pure.tool_message(tc.id, output))
                    elif tools and tc.name in tools:
                        output = self._run_pipeline_tool(
                            folder, worktree, tc.name, args, tools[tc.name], counter
                        )
                        messages.append(
                            openai_pure.tool_message(
                                tc.id, output, max_chars=mcp_pure.MAX_RESULT_CHARS
                            )
                        )
                    else:
                        unexpected[tc.name] = unexpected.get(tc.name, 0) + 1
                        output = sandbox.execute(tc.name, args or {})
                        messages.append(openai_pure.tool_message(tc.id, output))
            else:
                error = AIErrorKind.TIMEOUT
                detail = f"수정 턴 상한({MAX_FIX_TURNS})을 넘음"

            patch, rest = folder.patch(), folder.rest()

        applied = b""
        try:
            if rest:
                repo.apply_patch(worktree, rest)
            applied = patch
        except repo.GitError as e:
            log.warning("로컬 모델의 변경을 worktree에 적용하지 못함: %s", e)
            error = AIErrorKind.APPLY
            detail = f"변경을 적용하지 못함: {e}"
            applied = b""

        return EditResult(
            ok=error is None,
            error=error,
            cost_usd=0.0,
            num_turns=turns,
            permission_denials=tuple(sandbox.denials),
            text=last_text,
            detail=detail,
            unexpected_tools=tuple(sorted(unexpected.items())),
            tool_uses=sandbox.tool_uses + counter.usage().calls,
            model_id=model_id,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            applied=applied,
            tool_usage=counter.usage(),
            environment=environment,
        )

    def _run_pipeline_tool(
        self,
        folder: workdir.ModelFolder,
        worktree: Path,
        name: str,
        raw_args: Mapping[str, object] | None,
        handler: PipelineTool,
        counter: _PipelineCounter,
    ) -> str:
        narrowed = mcp_pure.tool_arguments(name, raw_args)
        if narrowed is None:
            return f"알 수 없는 도구나 인자: {name!r}"
        reason = mcp_pure.limit_reason(
            sum(counter.counts.values()), counter.seconds, counter.limits
        )
        if reason is not None:
            counter.limited = True
            return reason
        folder.sync(worktree)
        counter.counts[name] = counter.counts.get(name, 0) + 1
        started = time.monotonic()
        try:
            return handler(narrowed)
        except Exception as e:
            log.warning("파이프라인 도구 %s 실패: %s", name, e)
            return f"도구를 돌리지 못함: {type(e).__name__}"
        finally:
            counter.seconds += time.monotonic() - started

    def startup_check(self) -> StartupCheck:
        models = self._list_models(self.base_url)
        hint = f"{self.base_url}에 연결할 수 있게 Ollama 또는 llama-server를 실행하세요"
        return StartupCheck(SERVER_CHECK, models is not None, hint, "local_model")


def _structured_error(kind: AIErrorKind, detail: str) -> StructuredResult:
    return StructuredResult(
        ok=False, error=kind, data=None, model_id="", input_tokens=None,
        output_tokens=None, cost_usd=0.0, permission_denials=0, detail=detail,
    )  # fmt: skip


def _reading_result(
    *,
    ok: bool,
    error: AIErrorKind | None,
    data: Mapping[str, object] | None,
    model_id: str,
    in_tokens: int | None,
    out_tokens: int | None,
    sandbox: SandboxTools,
    unexpected: Mapping[str, int],
    turns: int,
    detail: str = "",
) -> StructuredResult:
    return StructuredResult(
        ok=ok,
        error=error,
        data=data,
        model_id=model_id,
        input_tokens=in_tokens,
        output_tokens=out_tokens,
        cost_usd=0.0,
        permission_denials=len(sandbox.denials),
        detail=detail,
        tool_uses=sandbox.tool_uses,
        reads=tuple(sorted(sandbox.reads)),
        unexpected_tools=tuple(sorted(unexpected.items())),
        num_turns=turns,
    )
