"""claude CLI의 rate_limit_event에서 사용량 한도를 읽는다."""

import json

from app.worker.model.usage_limit_pure import REPROBE_SECONDS, Limited, limited, until


def _event(status: str, resets: int | None = 1_795_692_000) -> str:
    info: dict[str, object] = {"status": status, "rateLimitType": "five_hour"}
    if resets is not None:
        info["resetsAt"] = resets
    return json.dumps({"type": "rate_limit_event", "rate_limit_info": info})


def test_rejected_event_means_limited_with_the_reset_time() -> None:
    out = "\n".join(['{"type":"system","subtype":"init"}', _event("rejected")])
    assert limited(out) == Limited(1_795_692_000.0)


def test_allowed_and_warning_events_are_not_limits() -> None:
    assert limited(_event("allowed")) is None
    assert limited(_event("allowed_warning")) is None
    assert limited('{"type":"result","result":"rate_limit_event"}') is None
    assert limited("깨진 줄 rate_limit_event {") is None


def test_the_last_event_decides() -> None:
    assert limited(_event("rejected") + "\n" + _event("allowed")) is None
    assert limited(_event("allowed") + "\n" + _event("rejected", None)) == Limited(None)


def test_until_uses_the_reset_time_or_a_reprobe_interval() -> None:
    assert until(Limited(2_000.0), now=1_000.0) == 2_000.0
    assert until(Limited(None), now=1_000.0) == 1_000.0 + REPROBE_SECONDS
    assert until(Limited(500.0), now=1_000.0) == 1_000.0 + REPROBE_SECONDS  # 이미 지난 시각


def test_cli_failure_with_a_rejected_event_becomes_a_usage_limit_error() -> None:
    from app.worker.model.claude.claude_cli import _usage_limit
    from app.worker.model.model_backend import AIErrorKind

    error, detail, resets = _usage_limit(_event("rejected"), AIErrorKind.EXIT, "종료 코드 1")
    assert error == AIErrorKind.USAGE_LIMIT and resets == 1_795_692_000.0
    assert "사용량 한도" in detail and "종료 코드 1" in detail
    # 한도에 닿았다는 줄이 있어도 성공한 호출, 안전 장치가 멈춘 호출은 그대로 둔다
    assert _usage_limit(_event("rejected"), None, "") == (None, "", None)
    assert _usage_limit(_event("rejected"), AIErrorKind.TOOLS, "d")[0] == AIErrorKind.TOOLS
    assert _usage_limit(_event("allowed"), AIErrorKind.EXIT, "d") == (AIErrorKind.EXIT, "d", None)


def test_cli_output_without_a_result_line_is_still_read_as_a_usage_limit() -> None:
    from app.worker.model.claude import claude_cli
    from app.worker.model.model_backend import AIErrorKind

    stdout = _event("rejected") + "\n"
    structured = claude_cli.parse_structured_output(stdout, 1)
    edited = claude_cli.parse_output(stdout, 1)
    assert structured.error == edited.error == AIErrorKind.USAGE_LIMIT
    assert structured.limit_resets_at == edited.limit_resets_at == 1_795_692_000.0
    assert claude_cli.parse_structured_output("", 1).error == AIErrorKind.BAD_JSON
