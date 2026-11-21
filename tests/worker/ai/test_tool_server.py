"""파이프라인 도구 서버와 stdio 중계 (docs/DESIGN.md 4.7, 8.4). 실제 소켓과 실제 중계 프로세스를
쓴다."""

import json
import subprocess
import sys
import time

from worker.ai import tool_server
from worker.mcp_pure import LIMIT_REACHED, ToolLimits


def _talk(server, *messages: dict) -> list[dict]:
    """claude CLI처럼 중계 스크립트를 띄워 줄을 보내고 답을 받는다."""
    config = json.loads(server.config)["mcpServers"]["proof"]
    proc = subprocess.run(
        [config["command"], *config["args"]],
        input="".join(json.dumps(m) + "\n" for m in messages),
        capture_output=True, text=True, timeout=30, check=True,
    )  # fmt: skip
    return [json.loads(line) for line in proc.stdout.splitlines()]


def _call(mid: int, name: str, args: dict | None = None) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "method": "tools/call",
            "params": {"name": name, "arguments": args or {}}}  # fmt: skip


def test_relay_carries_requests_to_the_handlers_and_back() -> None:
    seen = []
    handlers = {"check_rules": lambda a: seen.append("check") or "남은 위반 0건",
                "run_tests": lambda a: seen.append(a["code"]) or "통과"}  # fmt: skip
    with tool_server.serving(handlers) as server:
        replies = _talk(
            server,
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            _call(2, "check_rules"),
            _call(3, "run_tests", {"code": "before"}),
        )
        usage = server.usage()
    by_id = {r["id"]: r for r in replies}
    assert by_id[2]["result"]["content"][0]["text"] == "남은 위반 0건"
    assert by_id[3]["result"]["content"][0]["text"] == "통과"
    assert sorted(seen) == ["before", "check"]
    assert (usage.check_rules, usage.run_tests, usage.limited) == (1, 1, False)


def test_calls_over_the_limit_do_not_run_and_the_call_goes_on() -> None:
    runs = []
    with tool_server.serving({"check_rules": lambda a: runs.append(1) or "ok"},
                             ToolLimits(calls=1)) as server:  # fmt: skip
        first, second = server.call("check_rules", {}), server.call("check_rules", {})
        usage = server.usage()
    assert first == ("ok", False) and second[0].startswith(LIMIT_REACHED) and not second[1]
    assert runs == [1] and usage.limited and usage.calls == 1


def test_time_limit_counts_the_time_spent_in_tools() -> None:
    def slow(args):
        time.sleep(0.05)
        return "ok"

    with tool_server.serving({"run_tests": slow}, ToolLimits(seconds=0.01)) as server:
        server.call("run_tests", {"code": "now"})
        assert server.call("run_tests", {"code": "now"})[0].startswith(LIMIT_REACHED)
        assert server.usage().seconds >= 0.0


def test_a_failing_tool_answers_with_an_error_text() -> None:
    def broken(args):
        raise RuntimeError("docker 없음")

    with tool_server.serving({"run_tests": broken}) as server:
        text, failed = server.call("run_tests", {"code": "now"})
    assert failed and "RuntimeError" in text and "docker 없음" not in text


def test_relay_fails_cleanly_without_a_server(tmp_path) -> None:
    proc = subprocess.run([sys.executable, "-I", str(tool_server.RELAY), str(tmp_path / "none")],
                          input="", capture_output=True, text=True, timeout=10)  # fmt: skip
    assert proc.returncode == 1 and "붙지 못함" in proc.stderr
