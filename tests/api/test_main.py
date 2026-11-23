import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from redis import Redis
from redis import exceptions as redis_errors

from app.api import main as api
from app.api import signature
from app.api.sync import SyncResult
from app.shared import config
from app.shared.ops_alert_pure import TEXT

from ..redis_stand_ins import UNREACHABLE_REDIS_URL


@pytest.fixture
def client(tmp_path: Path, monkeypatch, test_redis_url: str, real_redis):
    """웹훅을 실제 Redis의 큐에 넣는다 (tests/conftest.py)."""
    (tmp_path / "config.yaml").write_text(
        f"repos: [Eunryong/eval]\nredis_url: {test_redis_url}\nsync:\n  interval_minutes: 0\n"
    )
    secret = tmp_path / "webhook_secret"
    secret.write_text("api-secret")
    secret.chmod(0o600)
    monkeypatch.setenv("PIPELINE_CONFIG_DIR", str(tmp_path))
    with TestClient(api.app) as c:
        yield c


def test_webhook_end_to_end(client: TestClient) -> None:
    payload = json.loads(
        (
            Path(__file__).parents[1] / "fixtures" / "webhooks" / "pull_request_opened.json"
        ).read_text()
    )
    body = json.dumps(payload).encode()
    headers = {
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": "d-api",
        "X-Hub-Signature-256": signature.sign("api-secret", body),
        "Content-Type": "application/json",
    }
    response = client.post("/webhook/github", content=body, headers=headers)
    assert response.status_code == 202
    assert response.json()["status"] == "queued"


def test_unsigned_webhook_is_rejected(client: TestClient) -> None:
    response = client.post("/webhook/github", content=b"{}", headers={"X-GitHub-Event": "ping"})
    assert response.status_code == 401


def test_healthz(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def _down(*args, **kwargs):
    raise redis_errors.ConnectionError("Redis가 꺼짐")


def _gh_login(*args, **kwargs) -> SyncResult:
    return SyncResult(failed_repos=["o/r"], environment=frozenset({"gh_login"}))


@pytest.mark.parametrize(("run_once", "kind"), [(_down, "redis"), (_gh_login, "gh_login")])
def test_sync_loop_notifies_once_from_the_second_failing_cycle(
    tmp_path: Path, monkeypatch, notifications, run_once, kind: str
) -> None:
    """6-4: 동기화가 본 Redis 꺼짐과 gh 로그인 실패는 둘째 주기부터 알리고, 같은 종류는 30분에
    한 번이다. 표시가 Redis가 아니라 data_dir의 파일이라 Redis가 꺼져도 센다."""
    settings = config.PipelineConfig(repos=["o/r"], data_dir=tmp_path)
    monkeypatch.setattr(api.sync, "run_once", run_once)
    seen: list[int] = []

    async def sleep(seconds: float) -> None:
        seen.append(len(notifications))
        if len(seen) == 3:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(api.sync_loop(settings, Redis.from_url(UNREACHABLE_REDIS_URL), sleep=sleep))
    assert seen == [0, 1, 1]
    assert notifications == [TEXT[kind]]
