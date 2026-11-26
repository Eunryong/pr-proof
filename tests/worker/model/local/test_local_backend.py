"""`LocalOpenAIBackend`와 `openai_client` 단위·루프백 HTTP 테스트."""

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from app.shared.config import ModelConfig
from app.worker.git import repo
from app.worker.model import backends
from app.worker.model.local import openai_client
from app.worker.model.local.local_backend import LocalOpenAIBackend
from app.worker.model.local.openai_client import LocalApiError
from app.worker.model.local.openai_pure import FINAL_ANSWER_PROMPT
from app.worker.model.mcp_pure import ToolLimits
from app.worker.model.model_backend import AIErrorKind, StartupCheck


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip


def _make_wt(tmp_path: Path) -> tuple[Path, Path]:
    outside = (tmp_path / "outside").resolve()
    outside.mkdir()
    (outside / "secret.txt").write_text("CANARY-123\n")
    wt = tmp_path / "wt"
    (wt / "src").mkdir(parents=True)
    _git(wt, "init", "-q", "-b", "main")
    repo.record_git_dir(wt)
    (wt / "src" / "app.py").write_text("X = 1\n")
    (wt / "notes.txt").symlink_to(outside / "secret.txt")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "init")
    return wt, outside


def _resp(
    content: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    *,
    model: str = "inkling-2-mini",
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return {
        "model": model,
        "choices": [{"finish_reason": "tool_calls" if tool_calls else "stop", "message": msg}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


def _tc(cid: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": cid,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
    }


def test_non_local_base_url_is_rejected() -> None:
    with pytest.raises(ValueError, match="로컬 모델 서버 주소가 아님"):
        LocalOpenAIBackend(base_url="https://api.openai.com/v1")


def test_backends_from_config_selects_local_openai() -> None:
    backend = backends.from_config(
        ModelConfig(backend="local-openai", base_url="http://127.0.0.1:11434/v1")
    )
    assert isinstance(backend, LocalOpenAIBackend)
    assert backend.base_url == "http://127.0.0.1:11434/v1"


def test_ask_returns_structured_data_and_falls_back_from_json_schema() -> None:
    seen_payloads: list[dict[str, Any]] = []

    def fake_post(base_url: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        seen_payloads.append(payload)
        if len(seen_payloads) == 1:
            raise LocalApiError(AIErrorKind.EXIT, "unsupported response_format", status=400)
        return _resp('<think>reasoning</think>\n{"answer": "ok"}')

    backend = LocalOpenAIBackend(post_chat=fake_post, model_override="custom-local")
    result = backend.ask(
        "질문",
        system_prompt="지시",
        schema={"type": "object", "properties": {"answer": {"type": "string"}}},
        model="sonnet",
        max_budget_usd=0.5,
    )
    assert result.ok and result.data == {"answer": "ok"}
    assert result.model_id == "inkling-2-mini"
    assert result.cost_usd == 0.0
    assert seen_payloads[0]["model"] == "custom-local"
    assert seen_payloads[0]["response_format"]["type"] == "json_schema"
    assert seen_payloads[1]["response_format"] == {"type": "json_object"}


def test_reading_executes_safe_tools_then_asks_final_answer_with_schema(tmp_path: Path) -> None:
    wt, outside = _make_wt(tmp_path)
    schema = {"type": "object", "properties": {"seen": {"type": "string"}}}
    turns = iter(
        [
            _resp(
                tool_calls=[
                    _tc("c1", "read_file", {"path": str(outside / "secret.txt")}),
                    _tc("c2", "read_file", {"path": "src/app.py"}),
                    _tc("c3", "bash", {"cmd": "id"}),
                ]
            ),
            _resp(content="읽기 끝"),
            _resp(content='{"seen": "X = 1"}'),
        ]
    )
    sent: list[dict[str, Any]] = []

    def post(base_url: str, payload: dict[str, Any], **kw: Any) -> dict[str, Any]:
        sent.append(payload)
        return next(turns)

    backend = LocalOpenAIBackend(post_chat=post)
    with backend.reading(wt) as ask:
        res = ask(
            "읽어라",
            system_prompt="지시",
            schema=schema,
            model="inkling-2-mini",
            max_budget_usd=0.5,
            max_tool_uses=5,
        )
    assert res.ok and res.data == {"seen": "X = 1"}
    # 읽는 턴에는 읽기 도구만 주고, 마지막 턴은 도구 없이 스키마를 강제한다
    assert [t["function"]["name"] for t in sent[0]["tools"]] == ["read_file", "grep", "glob"]
    assert "tools" not in sent[-1]
    assert sent[-1]["response_format"]["json_schema"]["schema"] == schema
    assert sent[-1]["messages"][-1] == {"role": "user", "content": FINAL_ANSWER_PROMPT}
    assert res.num_turns == 3
    assert res.reads == ("src/app.py",)
    assert res.tool_uses == 1
    assert res.permission_denials == 2  # outside read + bash
    assert res.unexpected_tools == (("bash", 1),)
    assert _git(wt, "status", "--porcelain") == ""


def test_reading_stops_at_max_tool_uses(tmp_path: Path) -> None:
    wt, _ = _make_wt(tmp_path)
    turns = iter(
        [
            _resp(tool_calls=[_tc("c1", "read_file", {"path": "src/app.py"})]),
            _resp(tool_calls=[_tc("c2", "read_file", {"path": "src/app.py"})]),
        ]
    )
    backend = LocalOpenAIBackend(post_chat=lambda *a, **kw: next(turns))
    with backend.reading(wt) as ask:
        res = ask(
            "읽어라",
            system_prompt="지시",
            schema={"type": "object"},
            model="inkling-2-mini",
            max_budget_usd=0.5,
            max_tool_uses=1,
        )
    assert not res.ok and res.error == AIErrorKind.TOOL_CAP


def test_fix_syncs_before_pipeline_tools_and_applies_diff(tmp_path: Path) -> None:
    wt, outside = _make_wt(tmp_path)
    seen_during_check: list[str] = []

    def check_rules(args: dict[str, str]) -> str:
        seen_during_check.append((wt / "src" / "app.py").read_text())
        return "위반 없음"

    turns = iter(
        [
            _resp(
                tool_calls=[
                    _tc("c1", "write_file", {"path": str(outside / "escape.txt"), "content": "x"}),
                    _tc(
                        "c2",
                        "edit_file",
                        {"path": "src/app.py", "old_string": "X = 1", "new_string": "X = 2"},
                    ),
                ]
            ),
            _resp(tool_calls=[_tc("c3", "check_rules", {})]),
            _resp(tool_calls=[_tc("c4", "check_rules", {})]),
            _resp("수정 완료"),
        ]
    )
    backend = LocalOpenAIBackend(
        post_chat=lambda *a, **kw: next(turns),
        tool_limits=ToolLimits(calls=1, seconds=60.0),
    )
    res = backend.fix(
        wt,
        "고쳐라",
        system_prompt="지시",
        model="inkling-2-mini",
        max_budget_usd=0.5,
        tools={"check_rules": check_rules},
    )
    assert res.ok and res.error is None and res.environment == ""
    assert res.text == "수정 완료"
    assert (wt / "src" / "app.py").read_text() == "X = 2\n"
    assert seen_during_check == ["X = 2\n"]  # sync가 파이프라인 도구 실행 전에 돌았다
    assert res.tool_usage.check_rules == 1 and res.tool_usage.limited is True
    assert len(res.permission_denials) == 1
    assert not (outside / "escape.txt").exists()
    assert b"X = 2" in res.applied


def test_fix_marks_connection_error_as_environment(tmp_path: Path) -> None:
    wt, _ = _make_wt(tmp_path)

    def fail(*a: Any, **kw: Any) -> dict[str, Any]:
        raise LocalApiError(AIErrorKind.LAUNCH, "connection refused", environment=True)

    backend = LocalOpenAIBackend(post_chat=fail)
    res = backend.fix(
        wt, "고쳐라", system_prompt="지시", model="inkling-2-mini", max_budget_usd=0.5
    )
    assert not res.ok and res.error == AIErrorKind.LAUNCH
    assert "connection refused" in res.environment


def test_timeout_defaults_to_and_is_capped_at_20_minutes() -> None:
    seen_timeouts: list[float] = []

    def fake_post(base_url: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        seen_timeouts.append(timeout)
        return _resp('{"ok": true}')

    backend = LocalOpenAIBackend(post_chat=fake_post)
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    backend.ask("q", system_prompt="s", schema=schema, model="m", max_budget_usd=0.1)
    backend.ask("q", system_prompt="s", schema=schema, model="m", max_budget_usd=0.1, timeout=1800)
    backend.ask("q", system_prompt="s", schema=schema, model="m", max_budget_usd=0.1, timeout=30)
    assert seen_timeouts == [1200.0, 1200.0, 30.0]


def test_startup_check_reports_local_server_status() -> None:
    ok_backend = LocalOpenAIBackend(list_models=lambda url: ["inkling-2-mini"])
    assert ok_backend.startup_check() == StartupCheck(
        "로컬 모델 서버",
        True,
        "http://127.0.0.1:8080/v1에 연결할 수 있게 Ollama 또는 llama-server를 실행하세요",
        "local_model",
    )
    down_backend = LocalOpenAIBackend(list_models=lambda url: None)
    assert down_backend.startup_check().ok is False


def test_openai_client_talks_to_real_loopback_http_server() -> None:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def do_GET(self) -> None:
            if self.path == "/v1/models":
                body = json.dumps({"data": [{"id": "inkling-2-mini"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

        def do_POST(self) -> None:
            if self.path == "/v1/chat/completions":
                length = int(self.headers.get("Content-Length", "0"))
                req = json.loads(self.rfile.read(length))
                body = json.dumps(_resp('{"echo": "' + req["model"] + '"}')).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{port}/v1"
        assert openai_client.list_models(base_url) == ["inkling-2-mini"]
        backend = LocalOpenAIBackend(base_url=base_url)
        res = backend.ask(
            "hi",
            system_prompt="sys",
            schema={"type": "object", "properties": {"echo": {"type": "string"}}},
            model="inkling-2-mini",
            max_budget_usd=0.1,
        )
        assert res.ok and res.data == {"echo": "inkling-2-mini"}
    finally:
        server.shutdown()
        server.server_close()
