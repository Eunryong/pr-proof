"""워커 시작 전 점검: GitHub 인증(gh 로그인 또는 토큰), claude 로그인, Docker, Redis
(docs/DESIGN.md 보안·인증 > Preflight). `npx pr-proof configure`의 끝 확인도 같은 함수를 쓴다."""

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from redis import Redis
from redis.exceptions import RedisError

from app.shared import github_auth
from app.shared import notify as macos
from app.shared.ops_alert_pure import Kind
from app.worker import github
from app.worker.ai.claude_cli import claude_env
from app.worker.sandbox.docker_cli import minimal_env


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    hint: str
    alert: Kind  # 실패하면 띄우는 운영 알림의 종류 (6-4)


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


def _gh_auth_ok() -> bool:
    """고른 인증(gh 로그인 또는 토큰)으로 `gh auth status`가 통과하는지. 토큰은 gh에만 넘긴다."""
    try:
        github.run_gh(github_auth.status_args())
    except github.GitHubError:
        return False
    return True


def run_checks(
    redis: Redis,
    command_ok: Callable[[list[str]], bool] = _command_ok,
    claude_status: Callable[[], str] = _claude_auth_status,
    now: datetime | None = None,
    gh_ok: Callable[[], bool] = _gh_auth_ok,
    config_dir: Path | None = None,
) -> list[Check]:
    claude_ok = claude_login_ok(claude_status(), now or datetime.now(UTC))
    return [
        Check(
            "GitHub 인증", gh_ok(), github_auth.hint(config_dir), github_auth.alert_kind(config_dir)
        ),
        Check("Claude 로그인", claude_ok, "claude 실행 후 /login", "claude_login"),
        Check("Docker", command_ok(["docker", "info"]), "Docker Desktop 실행", "docker"),
        Check("Redis", _redis_ok(redis), "docker compose up -d", "redis"),
    ]


def failures(checks: list[Check]) -> list[str]:
    return [f"{c.name} 실패 → {c.hint}" for c in checks if not c.ok]


def alert(checks: list[Check], data_dir: Path) -> None:
    """실패한 확인마다 macOS 알림을 띄운다. 같은 종류는 30분에 한 번이다(표시는 data_dir의 파일)."""
    for check in checks:
        if not check.ok:
            macos.send(check.alert, data_dir)
