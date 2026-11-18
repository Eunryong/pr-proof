import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Final

import pytest

from app.worker.ai import claude_cli as ai
from app.worker.ai.claude_cli import AIErrorKind

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


class FakeRunner:
    def __init__(self, stdout: str = "", returncode: int = 0, raises: Exception | None = None):
        self.stdout = stdout
        self.returncode = returncode
        self.raises = raises
        self.calls: list[tuple[list[str], str, Path, dict[str, str], float]] = []
        self.lines_seen = 0

    def __call__(self, args, stdin, cwd, env, timeout, watch):
        self.calls.append((args, stdin, cwd, env, timeout))
        if self.raises is not None:
            raise self.raises
        for line in self.stdout.splitlines():
            self.lines_seen += 1
            reason = watch(line)
            if reason is not None:
                raise ai.ToolListMismatch(reason)
        return subprocess.CompletedProcess(args, self.returncode, self.stdout, "")


def _run(runner: FakeRunner, **kwargs) -> ai.ClaudeResult:
    return ai.run_claude(
        Path("/work/pr-1"),
        kwargs.pop("prompt", "상수로 빼 주세요"),
        model=kwargs.pop("model", "haiku"),
        max_budget_usd=kwargs.pop("max_budget_usd", 0.3),
        runner=runner,
        environ=kwargs.pop("environ", {}),
        **kwargs,
    )


def test_invocation_is_restricted_and_pinned_to_worktree() -> None:
    runner = FakeRunner(_output())
    _run(runner, prompt="-p 로 시작하는 프롬프트", timeout=42)
    args, stdin, cwd, _, timeout = runner.calls[0]
    assert args == [
        "claude",
        "-p",
        "--restricted",
        "--strict-mcp-config",
        "--permission-mode",
        "dontAsk",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--output-format",
        "stream-json",
        "--verbose",
        # 도구 목록을 다섯 개로 줄인다. --allowedTools만으로는 WebSearch 등이 그대로 보인다
        "--tools",
        "Read,Grep,Glob,Write,Edit",
        "--allowedTools",
        "Read,Grep,Glob,Write,Edit",
        "--model",
        "haiku",
        "--max-budget-usd",
        "0.3000",
    ]
    assert stdin == "-p 로 시작하는 프롬프트"  # 프롬프트는 인자가 아니라 표준 입력
    assert cwd == Path("/work/pr-1")
    assert timeout == 42


def test_env_has_only_login_variables() -> None:
    environ = {
        "HOME": "/Users/me",
        "USER": "me",
        "PATH": "/usr/bin",
        "ANTHROPIC_API_KEY": "sk-secret",
        "PIPELINE_WEBHOOK_SECRET": "s",
        "GH_TOKEN": "t",
    }
    runner = FakeRunner(_output())
    _run(runner, environ=environ)
    assert runner.calls[0][3] == {"HOME": "/Users/me", "USER": "me", "PATH": "/usr/bin"}


def test_claude_env_passes_config_dir_when_set() -> None:
    assert ai.claude_env({"CLAUDE_CONFIG_DIR": "/c", "LANG": "C"}) == {"CLAUDE_CONFIG_DIR": "/c"}


def test_success() -> None:
    denial = {"tool_name": "Read", "tool_use_id": "x", "tool_input": {"file_path": "/etc/passwd"}}
    result = _run(FakeRunner(_output(permission_denials=[denial])))
    assert result.ok and result.error is None
    assert result.cost_usd == 0.0017
    assert result.num_turns == 3
    assert result.text == "고쳤습니다"
    assert result.permission_denials == (ai.PermissionDenial("Read", {"file_path": "/etc/passwd"}),)


def test_timeout() -> None:
    result = _run(FakeRunner(raises=subprocess.TimeoutExpired("claude", 1)), timeout=1)
    assert not result.ok
    assert result.error is AIErrorKind.TIMEOUT
    assert result.cost_usd is None  # 비용을 알 수 없음


def test_launch_failure() -> None:
    result = _run(FakeRunner(raises=FileNotFoundError("claude")))
    assert result.error is AIErrorKind.LAUNCH


def test_not_logged_in_is_exit_error() -> None:
    # 2.1.327에서 로그인을 찾지 못했을 때의 실제 출력: subtype success, is_error true, 종료 코드 1
    stdout = _output(is_error=True, total_cost_usd=0, num_turns=1, result="Not logged in")
    result = _run(FakeRunner(stdout, returncode=1))
    assert result.error is AIErrorKind.EXIT
    assert result.cost_usd == 0.0
    assert result.text == "Not logged in"


def test_is_error_with_zero_exit_is_failure() -> None:
    assert _run(FakeRunner(_output(is_error=True))).error is AIErrorKind.EXIT


def test_nonzero_exit_with_valid_json_keeps_cost() -> None:
    result = _run(FakeRunner(_output(subtype="error_during_execution"), returncode=1))
    assert result.error is AIErrorKind.EXIT
    assert result.cost_usd == 0.0017


def test_budget_exceeded() -> None:
    stdout = _output(subtype="error_max_budget_usd", is_error=True, total_cost_usd=0.31)
    result = _run(FakeRunner(stdout, returncode=1))
    assert result.error is AIErrorKind.BUDGET
    assert result.cost_usd == 0.31


@pytest.mark.parametrize("stdout", ["", "not json", "[1, 2]", '"text"', '{"a": '])
def test_bad_json(stdout: str) -> None:
    result = _run(FakeRunner(stdout, returncode=0))
    assert result.error is AIErrorKind.BAD_JSON
    assert result.cost_usd is None


def test_missing_fields_are_defaulted() -> None:
    result = ai.parse_output(_stream({"type": "result"}, FILE_TOOLS), 0)
    assert result.ok
    assert (result.cost_usd, result.num_turns, result.text) == (None, 0, "")
    assert result.permission_denials == ()


def test_malformed_denials_are_skipped() -> None:
    raw = _output(permission_denials=["x", {"tool_name": "Write", "tool_input": "bad"}])
    result = ai.parse_output(raw, 0)
    assert result.permission_denials == (ai.PermissionDenial("Write", {}),)


@pytest.mark.parametrize(
    ("model", "budget"),
    [
        ("", 0.3),
        ("--dangerously-skip-permissions", 0.3),
        ("haiku", 0),
        ("haiku", -1),
        ("haiku", 0.00009),
    ],
)
def test_invalid_arguments_rejected(model: str, budget: float) -> None:
    runner = FakeRunner(_output())
    with pytest.raises(ValueError):
        _run(runner, model=model, max_budget_usd=budget)
    assert runner.calls == []


def test_minimum_budget_accepted() -> None:
    runner = FakeRunner(_output())
    _run(runner, max_budget_usd=ai.MIN_BUDGET_USD)
    assert runner.calls[0][0][-1] == "0.0001"


SCHEMA: Final = {"type": "object", "properties": {"rules": {"type": "array"}}}


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


class CwdRecordingRunner(FakeRunner):
    """호출 순간의 cwd 내용을 남긴다. 호출이 끝난 뒤 그 폴더가 남는지도 본다."""

    def __call__(self, args, stdin, cwd, env, timeout, watch):
        self.cwd_entries = sorted(p.name for p in cwd.iterdir())
        return super().__call__(args, stdin, cwd, env, timeout, watch)


def _structured(runner: FakeRunner, **kwargs) -> ai.StructuredResult:
    return ai.run_structured(
        kwargs.pop("prompt", "문서"),
        system_prompt=kwargs.pop("system_prompt", "규칙을 뽑는다"),
        schema=kwargs.pop("schema", SCHEMA),
        model=kwargs.pop("model", "haiku"),
        max_budget_usd=kwargs.pop("max_budget_usd", 0.3),
        runner=runner,
        environ=kwargs.pop("environ", {"HOME": "/h", "GH_TOKEN": "t"}),
        **kwargs,
    )


def test_no_tools_call_has_no_tools_and_runs_in_an_empty_temp_dir() -> None:
    runner = CwdRecordingRunner(_structured_output())
    _structured(runner, prompt="--문서 본문", system_prompt="지시", timeout=7)
    args, stdin, cwd, env, timeout = runner.calls[0]
    assert args == [
        "claude", "-p", "--restricted", "--strict-mcp-config", "--permission-mode", "dontAsk",
        "--disable-slash-commands", "--no-session-persistence",
        "--output-format", "stream-json", "--verbose",
        "--tools", "",
        "--append-system-prompt", "지시",
        "--json-schema", json.dumps(SCHEMA),
        "--model", "haiku", "--max-budget-usd", "0.3000",
    ]  # fmt: skip
    assert stdin == "--문서 본문"  # 믿지 않는 자료는 표준 입력으로만
    assert runner.cwd_entries == []
    assert not cwd.exists()  # 호출이 끝나면 지운다
    assert env == {"HOME": "/h"}
    assert timeout == 7


def test_structured_answer_usage_and_model_id() -> None:
    result = _structured(FakeRunner(_structured_output(structured_output={"rules": [1]})))
    assert result.ok and result.error is None
    assert result.data == {"rules": [1]}
    assert result.model_id == "claude-haiku-5-5"
    assert (result.input_tokens, result.output_tokens) == (2 + 3672 + 100, 40)
    assert result.cost_usd == 0.0008


def test_falls_back_to_result_text_when_structured_output_is_missing() -> None:
    stdout = _structured_output(structured_output=None, result='{"rules": [2]}')
    assert _structured(FakeRunner(stdout)).data == {"rules": [2]}


@pytest.mark.parametrize("result_text", ["규칙이 없습니다", "[1, 2]", "", None])
def test_answer_that_is_not_a_json_object_is_bad_json(result_text: str | None) -> None:
    stdout = _structured_output(structured_output=None, result=result_text)
    result = _structured(FakeRunner(stdout))
    assert not result.ok and result.error is AIErrorKind.BAD_JSON and result.data is None
    assert result.model_id == "claude-haiku-5-5"  # 실패해도 기록할 값은 남긴다


def test_unknown_model_is_exit_error_even_with_success_subtype() -> None:
    stdout = _structured_output(is_error=True, structured_output=None, result="model not found",
                                modelUsage={})  # fmt: skip
    result = _structured(FakeRunner(stdout, returncode=1))
    assert result.error is AIErrorKind.EXIT and result.data is None and result.model_id == ""


def test_structured_budget_timeout_and_launch_failures() -> None:
    budget = _structured_output(subtype="error_max_budget_usd", is_error=True)
    assert _structured(FakeRunner(budget, returncode=1)).error is AIErrorKind.BUDGET
    timeout = _structured(FakeRunner(raises=subprocess.TimeoutExpired("claude", 1)))
    assert timeout.error is AIErrorKind.TIMEOUT and timeout.input_tokens is None
    assert _structured(FakeRunner(raises=FileNotFoundError())).error is AIErrorKind.LAUNCH
    assert _structured(FakeRunner("not json")).error is AIErrorKind.BAD_JSON


def test_model_id_is_the_model_with_most_output() -> None:
    usage = {
        "claude-haiku-5-5": {"outputTokens": 5},
        "claude-sonnet-5-5": {"outputTokens": 50},
        "broken": "x",
    }
    assert _structured(FakeRunner(_structured_output(modelUsage=usage))).model_id == (
        "claude-sonnet-5-5"
    )


def test_denials_are_counted() -> None:
    denial = {"tool_name": "Read", "tool_input": {"file_path": "/etc/hosts"}}
    stdout = _structured_output(permission_denials=[denial, denial])
    assert _structured(FakeRunner(stdout)).permission_denials == 2


@pytest.mark.parametrize("system_prompt", ["", "--dangerously-skip-permissions"])
def test_invalid_system_prompt_is_rejected(system_prompt: str) -> None:
    runner = FakeRunner(_structured_output())
    with pytest.raises(ValueError):
        _structured(runner, system_prompt=system_prompt)
    assert runner.calls == []


# --- 도구 목록 확인 ---


@pytest.mark.parametrize(
    "tools",
    [
        [*FILE_TOOLS, "WebSearch"],
        [*FILE_TOOLS, "Task", "Skill"],
        ["Read", "Grep", "Glob", "Write"],
        [],
    ],
)
def test_file_tool_call_stops_when_the_tool_list_is_not_exactly_the_five(tools: list[str]) -> None:
    runner = FakeRunner(_output(tools=tools))
    result = _run(runner)
    assert not result.ok and result.error == AIErrorKind.TOOLS
    assert "도구 목록" in result.detail
    assert runner.lines_seen == 1  # init 줄에서 멈춘다. 모델의 답은 읽지 않는다


def test_file_tool_call_accepts_the_five_in_any_order() -> None:
    assert _run(FakeRunner(_output(tools=["Write", "Read", "Edit", "Grep", "Glob"]))).ok


@pytest.mark.parametrize("tools", [["StructuredOutput", "Read"], ["WebSearch"], FILE_TOOLS])
def test_no_tools_call_stops_when_any_other_tool_is_listed(tools: list[str]) -> None:
    runner = FakeRunner(_structured_output(tools=tools))
    result = _structured(runner)
    assert not result.ok and result.error == AIErrorKind.TOOLS and result.data is None
    assert runner.lines_seen == 1


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


def _fake_claude(tmp_path: Path, body: str) -> list[str]:
    script = tmp_path / "fake_claude.py"
    script.write_text(body)
    return [sys.executable, str(script)]


def test_real_runner_kills_the_process_at_a_wrong_init(tmp_path: Path) -> None:
    marker = tmp_path / "reached"
    args = _fake_claude(
        tmp_path,
        "import json, sys, time, pathlib\n"
        "sys.stdin.read()\n"
        'print(json.dumps({"type": "system", "subtype": "init", "tools": ["Read", "WebSearch"]}),'
        " flush=True)\n"
        "time.sleep(5)\n"
        f"pathlib.Path({str(marker)!r}).write_text('x')\n",
    )
    started = time.monotonic()
    with pytest.raises(ai.ToolListMismatch, match="WebSearch"):
        ai._run(args, "프롬프트", tmp_path, dict(os.environ), 30, ai._watch_for("file_tools"))
    assert time.monotonic() - started < 4
    assert not marker.exists()


def test_real_runner_returns_output_and_times_out(tmp_path: Path) -> None:
    echo = _fake_claude(tmp_path, "import sys\nprint(sys.stdin.read().upper())\n")
    done = ai._run(echo, "abc", tmp_path, dict(os.environ), 30, lambda line: None)
    assert (done.returncode, done.stdout.strip()) == (0, "ABC")
    slow = _fake_claude(tmp_path, "import time\ntime.sleep(30)\n")
    with pytest.raises(subprocess.TimeoutExpired):
        ai._run(slow, "", tmp_path, dict(os.environ), 0.5, lambda line: None)


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


def test_mismatch_detail_names_extra_tools_and_cli_version() -> None:
    init = {
        "type": "system", "subtype": "init", "claude_code_version": "2.1.327",
        "tools": [*FILE_TOOLS, "WebSearch", "Task"],
    }  # fmt: skip
    result = _run(FakeRunner(json.dumps(init) + "\n"))
    assert result.error == AIErrorKind.TOOLS
    assert "['Task', 'WebSearch']" in result.detail and "claude CLI 2.1.327" in result.detail
