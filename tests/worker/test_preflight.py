import json
from datetime import UTC, datetime
from typing import Final

import fakeredis
import pytest

from app.worker import preflight

NOW: Final = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


def _status(**fields: object) -> str:
    return json.dumps({"loggedIn": True, "loginExpiresAt": "2026-12-07T23:00:25.456Z", **fields})


def _checks(command_ok=lambda args: True, claude_status=_status):
    return preflight.run_checks(
        fakeredis.FakeStrictRedis(), command_ok=command_ok, claude_status=claude_status, now=NOW
    )


def test_all_checks_pass() -> None:
    assert preflight.failures(_checks()) == []


def test_failures_include_hint() -> None:
    def command_ok(args: list[str]) -> bool:
        return args[0] != "docker"

    assert preflight.failures(_checks(command_ok=command_ok)) == [
        "Docker 실패 → Docker Desktop 실행"
    ]


def test_claude_login_failure_has_hint() -> None:
    assert preflight.failures(_checks(claude_status=lambda: _status(loggedIn=False))) == [
        "Claude 로그인 실패 → claude 실행 후 /login"
    ]


@pytest.mark.parametrize(
    ("status", "ok"),
    [
        (_status(), True),
        (_status(loginExpiresAt=None), True),  # 만료 시각이 없는 로그인 방식
        (_status(loginExpiresAt="2026-11-18T12:00:01+00:00"), True),  # 1초 남음
        (_status(loginExpiresAt="2026-11-18T12:00:00+00:00"), False),  # 지금 만료
        (_status(loginExpiresAt="2026-11-01T00:00:00Z"), False),
        (_status(loggedIn=False, loginExpiresAt=None), False),
        (_status(loggedIn="true"), False),  # 문자열은 참으로 보지 않음
        (json.dumps({"loginExpiresAt": None}), False),  # loggedIn 없음
        (_status(loginExpiresAt="내일"), False),
        (_status(loginExpiresAt="2026-12-07T23:00:25"), False),  # 시간대 없음
        (_status(loginExpiresAt=1765148425), False),
        ("", False),  # claude를 실행하지 못함
        ("Not logged in", False),
        ("[]", False),
    ],
)
def test_claude_login_ok(status: str, ok: bool) -> None:
    assert preflight.claude_login_ok(status, NOW) is ok
