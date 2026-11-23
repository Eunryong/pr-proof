"""app/worker/ai/claude_backend.py: claude CLI 구현. 실제 git과 가짜 실행기로 보고 모델은 부르지
않는다.

정리 전에는 호출하는 쪽이 `claude_cli.run_structured`, `workdir.reading_folder`,
`workdir.run_in_model_folder`를 바로 불렀다. 구현을 거쳐도 claude에 넘기는 인자(명령 인자, 표준
입력, 환경 변수, 시간 상한)가 그 호출과 글자 하나까지 같아야 한다.
"""

import json
import re
import subprocess
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Final

import pytest

from app.worker import mcp_pure, repo
from app.worker.ai import backends, claude_backend, claude_cli, workdir
from app.worker.ai.claude_backend import ClaudeCliBackend
from app.worker.ai.model_backend import (
    AIErrorKind,
    Capabilities,
    EditResult,
    StartupCheck,
    StructuredResult,
    fix_unavailable,
)

FILE_TOOLS: Final = ["Read", "Grep", "Glob", "Write", "Edit"]
SCHEMA: Final = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
ASK: Final = {"system_prompt": "지시", "schema": SCHEMA, "model": "haiku", "max_budget_usd": 0.05}
READ: Final = {**ASK, "max_tool_uses": 20}
FIX: Final = {"system_prompt": "고쳐", "model": "sonnet", "max_budget_usd": 1.5, "timeout": 1800.0}
NOW: Final = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd, check=True, capture_output=True,
    )  # fmt: skip


@pytest.fixture
def wt(tmp_path: Path) -> Path:
    path = tmp_path / "wt"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    repo.record_git_dir(path)
    (path / "a.py").write_text("X = 1\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    return path


class Recorder:
    """가짜 실행기: 모양에 맞는 도구 목록과 답을 내고, 받은 인자를 남긴다."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, args, stdin, cwd, env, timeout, watch):
        if "--json-schema" in args:
            tools = ["StructuredOutput"]
            if "Read,Grep,Glob" in args:
                tools = ["Read", "Grep", "Glob", *tools]
        elif "--mcp-config" in args:
            tools = [*FILE_TOOLS, *mcp_pure.TOOL_NAMES]
        else:
            tools = FILE_TOOLS
        result = {
            "type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
            "result": '{"ok": true}', "structured_output": {"ok": True}, "permission_denials": [],
        }  # fmt: skip
        out = "".join(
            json.dumps(e) + "\n"
            for e in ({"type": "system", "subtype": "init", "tools": tools}, result)
        )
        for line in out.splitlines():
            assert watch(line) is None
        self.calls.append({"args": args, "stdin": stdin, "cwd": str(cwd), "env": env,
                           "timeout": timeout})  # fmt: skip
        return subprocess.CompletedProcess(args, 0, out, "")


# 호출마다 새로 만드는 임시 폴더 (빈 폴더, 모델 폴더, 도구 서버의 소켓 폴더)
_TEMP: Final = re.compile(r"[^\s\"']*/(claude-no-tools-|claude-model-|pp-tools-)[\w]+")


def _normalized(calls: list[dict]) -> str:
    """호출마다 새로 만드는 폴더의 임시 경로만 지운다."""
    return _TEMP.sub(r"<\1>", json.dumps(calls, ensure_ascii=False))


def _tools() -> dict:
    return {mcp_pure.CHECK_RULES: lambda a: "검사", mcp_pure.RUN_TESTS: lambda a: "테스트"}


def test_the_backend_calls_claude_exactly_like_the_direct_calls(wt: Path) -> None:
    """도구 없는 물음, 읽기 폴더의 두 물음, 파이프라인 도구를 준 고치기, 도구 없는 고치기."""
    before, after = Recorder(), Recorder()
    ask = partial(claude_cli.run_structured, runner=before)
    ask("자료", **ASK)
    with workdir.reading_folder(wt, ask) as read:
        read("읽어라", **READ)
        read("또", **READ)
    workdir.run_in_model_folder(wt, "고칠 것", tools=_tools(), runner=before, **FIX)
    workdir.run_in_model_folder(wt, "고칠 것", runner=before, system_prompt="s", model="haiku",
                                max_budget_usd=0.3)  # fmt: skip

    backend = ClaudeCliBackend(
        run_structured=partial(claude_cli.run_structured, runner=after),
        run_claude=partial(claude_cli.run_claude, runner=after),
    )
    backend.ask("자료", **ASK)
    with backend.reading(wt) as read:
        read("읽어라", **READ)
        read("또", **READ)
    backend.fix(wt, "고칠 것", tools=_tools(), **FIX)
    backend.fix(wt, "고칠 것", system_prompt="s", model="haiku", max_budget_usd=0.3)

    assert len(after.calls) == 5
    assert _normalized(after.calls) == _normalized(before.calls)
    timeouts = [c["timeout"] for c in after.calls]
    assert timeouts == [600.0, 600.0, 600.0, 1800.0, 600.0]
    assert after.calls[3]["env"]["MCP_TOOL_TIMEOUT"] == claude_cli.MCP_TOOL_TIMEOUT_MS


def test_ask_passes_only_what_the_caller_gave() -> None:
    """가짜 run_structured를 쓰는 테스트가 받던 인자 그대로다. 시간 상한은 준 때만 넘긴다."""
    seen: list[tuple[str, dict]] = []

    def run(prompt: str, **kwargs: object) -> StructuredResult:
        seen.append((prompt, kwargs))
        return StructuredResult(True, None, {}, "", None, None, None, 0)

    backend = ClaudeCliBackend(run_structured=run)
    backend.ask("p", **ASK)
    backend.ask("p", **ASK, timeout=5.0)
    assert seen == [("p", ASK), ("p", {**ASK, "timeout": 5.0})]


def test_the_claude_call_is_looked_up_when_called(monkeypatch, wt: Path) -> None:
    """구현을 만든 뒤에 `claude_cli.run_structured`·`run_claude`를 바꿔 끼워도 그것이 불린다."""
    backend = ClaudeCliBackend()
    asked: list[dict] = []
    fixed: list[Path] = []

    def ask(prompt: str, **kwargs: object) -> StructuredResult:
        asked.append(kwargs)
        return StructuredResult(True, None, {"ok": True}, "", None, None, None, 0)

    def fix(work: Path, prompt: str, **kwargs: object) -> EditResult:
        fixed.append(work)
        return EditResult(True, None, None, 1, (), "")

    monkeypatch.setattr(claude_cli, "run_structured", ask)
    monkeypatch.setattr(claude_cli, "run_claude", fix)
    assert backend.ask("p", **ASK).data == {"ok": True}
    with backend.reading(wt) as read:
        read("p", **READ)
    assert backend.fix(wt, "p", system_prompt="s", model="m", max_budget_usd=1).ok
    assert "read_in" not in asked[0]
    assert isinstance(asked[1]["read_in"], claude_cli.ReadFolder)
    assert fixed and fixed[0] != wt  # 모델 폴더에서 돈다


@pytest.mark.parametrize(
    ("result", "environment"),
    [
        (EditResult(True, None, None, 1, (), "고침"), ""),
        (EditResult(False, AIErrorKind.EXIT, None, 0, (), "Not logged in · Please run /login"),
         "claude 로그인이 풀림"),
        (EditResult(False, AIErrorKind.LAUNCH, None, 0, (), "", detail="claude 없음"),
         "claude를 실행하지 못함: claude 없음"),
        (EditResult(False, AIErrorKind.TIMEOUT, None, 0, (), ""), ""),
    ],
)  # fmt: skip
def test_fix_reports_an_unusable_environment(wt: Path, result: EditResult, environment) -> None:
    """로그인 풀림 같은 환경 오류는 claude의 글자를 아는 구현이 결과의 environment로 알린다."""
    backend = ClaudeCliBackend(run_claude=lambda work, prompt, **kw: result)
    out = backend.fix(wt, "p", system_prompt="s", model="m", max_budget_usd=1)
    assert out.environment == environment
    assert (out.ok, out.error) == (result.ok, result.error)


@pytest.mark.parametrize(
    ("status", "ok"),
    [
        (json.dumps({"loggedIn": True, "loginExpiresAt": "2026-12-07T23:00:25.456Z"}), True),
        (json.dumps({"loggedIn": True, "loginExpiresAt": "2026-11-01T00:00:00Z"}), False),
        ("", False),
    ],
)
def test_startup_check_is_the_claude_login(status: str, ok: bool) -> None:
    """이름, 안내 글, 알림 종류는 정리 전의 "Claude 로그인" 확인과 같다."""
    backend = ClaudeCliBackend(auth_status=lambda: status, clock=lambda: NOW)
    assert backend.startup_check() == StartupCheck(
        "Claude 로그인", ok, "claude 실행 후 /login", "claude_login"
    )


def test_claude_can_do_everything_the_pipeline_asks() -> None:
    backend = ClaudeCliBackend()
    assert backend.name == "claude-cli"
    assert backend.capabilities == Capabilities(
        reads_code=True, reports_reads=True, edits=True, pipeline_tools=True
    )
    assert fix_unavailable(backend) == ""


class _NoEdit(ClaudeCliBackend):
    capabilities = Capabilities(reads_code=True, reports_reads=True, edits=False,
                                pipeline_tools=False)  # fmt: skip


class _NoTools(ClaudeCliBackend):
    capabilities = Capabilities(reads_code=True, reports_reads=True, edits=True,
                                pipeline_tools=False)  # fmt: skip


def test_fix_unavailable_names_what_the_backend_cannot_do() -> None:
    assert fix_unavailable(_NoEdit()) == "모델 구현 claude-cli은 코드를 고치지 못한다"
    assert fix_unavailable(_NoTools()) == (
        "모델 구현 claude-cli은 고치는 동안 규칙 검사·테스트 도구를 주지 못한다"
    )


def test_backends_are_chosen_by_name() -> None:
    assert isinstance(backends.make_backend("claude-cli"), ClaudeCliBackend)
    assert backends.default_backend().name == backends.DEFAULT_BACKEND == claude_backend.NAME


@pytest.mark.parametrize("name", ["openai", "", "Claude-CLI"])
def test_an_unknown_backend_fails_with_the_known_names(name: str) -> None:
    with pytest.raises(backends.UnknownBackend) as caught:
        backends.make_backend(name)
    assert str(caught.value) == f"모르는 모델 구현 {name!r}: 쓸 수 있는 것은 claude-cli"
