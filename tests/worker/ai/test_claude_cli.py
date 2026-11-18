import json
import subprocess
from pathlib import Path

import pytest

from app.worker.ai import claude_cli as ai
from app.worker.ai.claude_cli import AIErrorKind


def _output(**fields: object) -> str:
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
    return json.dumps(data)


class FakeRunner:
    def __init__(self, stdout: str = "", returncode: int = 0, raises: Exception | None = None):
        self.stdout = stdout
        self.returncode = returncode
        self.raises = raises
        self.calls: list[tuple[list[str], str, Path, dict[str, str], float]] = []

    def __call__(self, args, stdin, cwd, env, timeout):
        self.calls.append((args, stdin, cwd, env, timeout))
        if self.raises is not None:
            raise self.raises
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
        "--allowedTools",
        "Read,Grep,Glob,Write,Edit",
        "--output-format",
        "json",
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
    result = ai.parse_output(json.dumps({"type": "result"}), 0)
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
