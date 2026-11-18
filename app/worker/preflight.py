"""워커 시작 전 점검: gh·claude 로그인, Docker, Redis (docs/DESIGN.md 보안·인증 > Preflight)."""

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from redis import Redis
from redis.exceptions import RedisError

from app.sandbox.docker_cli import minimal_env
from app.worker.ai.claude_cli import claude_env


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    hint: str


def _command_ok(args: list[str]) -> bool:
    try:
        return (
            subprocess.run(
                args, capture_output=True, env=minimal_env(), timeout=30, check=False
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


def _redis_ok(redis: Redis) -> bool:
    try:
        return bool(redis.ping())
    except RedisError:
        return False


def _claude_auth_status() -> str:
    """`claude auth status`의 JSON 출력. 비용이 들지 않는다. 실행하지 못하면 빈 문자열."""
    try:
        return subprocess.run(
            ["claude", "auth", "status"],
            capture_output=True,
            text=True,
            env=claude_env(),
            timeout=30,
            check=False,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def claude_login_ok(status_json: str, now: datetime) -> bool:
    """`loggedIn`이 참이고 `loginExpiresAt`이 지나지 않았으면 통과.

    만료 시각이 없으면(null) 로그인만 본다. 형식을 읽지 못하면 실패로 본다.
    """
    try:
        data = json.loads(status_json)
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict) or data.get("loggedIn") is not True:
        return False
    expires = data.get("loginExpiresAt")
    if expires is None:
        return True
    if not isinstance(expires, str):
        return False
    try:
        expires_at = datetime.fromisoformat(expires)
    except ValueError:
        return False
    if expires_at.tzinfo is None:
        return False
    return expires_at > now


def run_checks(
    redis: Redis,
    command_ok: Callable[[list[str]], bool] = _command_ok,
    claude_status: Callable[[], str] = _claude_auth_status,
    now: datetime | None = None,
) -> list[Check]:
    claude_ok = claude_login_ok(claude_status(), now or datetime.now(UTC))
    return [
        Check("GitHub 로그인", command_ok(["gh", "auth", "status"]), "gh auth login"),
        Check("Claude 로그인", claude_ok, "claude 실행 후 /login"),
        Check("Docker", command_ok(["docker", "info"]), "Docker Desktop 실행"),
        Check("Redis", _redis_ok(redis), "docker compose up -d"),
    ]


def failures(checks: list[Check]) -> list[str]:
    return [f"{c.name} 실패 → {c.hint}" for c in checks if not c.ok]
