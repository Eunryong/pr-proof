import json
from datetime import UTC, datetime
from typing import Final

import pytest
from redis import Redis

from app.shared.ops_alert_pure import TEXT
from app.worker import preflight

from ..redis_stand_ins import UNREACHABLE_REDIS_URL, Pong

NOW: Final = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


def _status(**fields: object) -> str:
    return json.dumps({"loggedIn": True, "loginExpiresAt": "2026-12-07T23:00:25.456Z", **fields})


def _checks(command_ok=lambda args: True, claude_status=_status, gh_ok=lambda: True):
    return preflight.run_checks(
        Pong(), command_ok=command_ok, claude_status=claude_status, now=NOW,
        gh_ok=gh_ok,
    )  # fmt: skip


def test_all_checks_pass() -> None:
    assert preflight.failures(_checks()) == []


def test_failures_include_hint() -> None:
    def command_ok(args: list[str]) -> bool:
        return args[0] != "docker"

    assert preflight.failures(_checks(command_ok=command_ok)) == [
        "Docker 실패 → Docker Desktop 실행"
    ]


def test_each_failed_check_shows_its_own_line(tmp_path, notifications) -> None:
    """6-4: 실패한 확인마다 그 종류의 정한 한 줄이 뜬다. 통과한 확인은 뜨지 않는다."""
    checks = preflight.run_checks(
        Redis.from_url(UNREACHABLE_REDIS_URL),
        command_ok=lambda args: True,
        claude_status=lambda: "",
        now=NOW,
        gh_ok=lambda: False,
    )
    preflight.alert(checks, tmp_path)
    assert notifications == [TEXT["gh_login"], TEXT["claude_login"], TEXT["redis"]]
    notifications.clear()
    preflight.alert(_checks(), tmp_path)
    assert notifications == []


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
