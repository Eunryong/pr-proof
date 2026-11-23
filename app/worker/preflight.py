"""워커 시작 전 점검: GitHub 인증(gh 로그인 또는 토큰), 모델 구현의 시작 확인(claude 구현은 claude
로그인), Docker, Redis (docs/DESIGN.md 보안·인증 > Preflight). `npx pr-proof configure`의 끝 확인도
같은 함수를 쓴다."""

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from redis import Redis
from redis.exceptions import RedisError

from app.shared import github_auth
from app.shared import notify as macos
from app.shared.ops_alert_pure import Kind
from app.worker.git import github
from app.worker.model import backends
from app.worker.model.claude import claude_backend
from app.worker.model.model_backend import ModelBackend
from app.worker.sandbox.docker_cli import minimal_env

# `app/setup/configure.py`(설치 명령의 Claude 로그인 단계)가 부르는 이름. 정의는 claude 구현에 있다
claude_login_ok = claude_backend.login_ok
_claude_auth_status = claude_backend.auth_status


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
    gh_ok: Callable[[], bool] = _gh_auth_ok,
    config_dir: Path | None = None,
    backend: ModelBackend | None = None,
) -> list[Check]:
    """backend는 확인할 모델 구현이다. 없으면 워커가 쓰는 구현(`backends.default_backend`)."""
    model = (backend or backends.default_backend()).startup_check()
    return [
        Check(
            "GitHub 인증", gh_ok(), github_auth.hint(config_dir), github_auth.alert_kind(config_dir)
        ),
        Check(model.name, model.ok, model.hint, model.alert),
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
