import json
from pathlib import Path

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app import api
from app.services import signature


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    (tmp_path / "config.yaml").write_text("repos: [Eunryong/eval]\n")
    secret = tmp_path / "webhook_secret"
    secret.write_text("api-secret")
    secret.chmod(0o600)
    monkeypatch.setenv("PIPELINE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(
        api,
        "Redis",
        type("R", (), {"from_url": staticmethod(lambda url: fakeredis.FakeStrictRedis())}),
    )
    with TestClient(api.app) as c:
        yield c


def test_webhook_end_to_end(client: TestClient) -> None:
    payload = json.loads(
        (Path(__file__).parent / "fixtures" / "webhooks" / "pull_request_opened.json").read_text()
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
