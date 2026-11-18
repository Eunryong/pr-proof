"""워커 시작 전 점검: gh 로그인, Docker, Redis (docs/DESIGN.md 보안·인증 > Preflight)."""

import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from redis import Redis
from redis.exceptions import RedisError

from app.sandbox.docker_cli import minimal_env


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


def run_checks(redis: Redis, command_ok: Callable[[list[str]], bool] = _command_ok) -> list[Check]:
    return [
        Check("GitHub 로그인", command_ok(["gh", "auth", "status"]), "gh auth login"),
        Check("Docker", command_ok(["docker", "info"]), "Docker Desktop 실행"),
        Check("Redis", _redis_ok(redis), "docker compose up -d"),
    ]


def failures(checks: list[Check]) -> list[str]:
    return [f"{c.name} 실패 → {c.hint}" for c in checks if not c.ok]
