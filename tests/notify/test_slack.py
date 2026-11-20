"""Slack 운영 알림 (완료 기준 6-4). 로컬 https 가짜 엔드포인트로 보낸다. 실제 Slack에는 보내지
않는다."""

import json
import shutil
import ssl
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import get_args

import fakeredis
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.models.ops_alert import TEXT, WINDOW_SECONDS, Alert, Kind, message
from app.notify import slack


class Endpoint:
    """Slack Incoming Webhook 대신 받은 본문을 모은다. `status`와 `redirect`로 응답을 바꾼다."""

    def __init__(self, server: ThreadingHTTPServer, context: ssl.SSLContext, cert: Path) -> None:
        self.server, self.client_context, self.cert = server, context, cert
        self.bodies: list[dict] = []
        self.status = 200
        self.redirect = ""

    @property
    def url(self) -> str:
        return f"https://127.0.0.1:{self.server.server_address[1]}/services/T/B/x"


@pytest.fixture
def endpoint(tmp_path: Path) -> Iterator[Endpoint]:
    if shutil.which("openssl") is None:
        pytest.skip("openssl이 없다")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
         "-out", str(cert), "-days", "1", "-subj", "/CN=127.0.0.1",
         "-addext", "subjectAltName=IP:127.0.0.1"],
        check=True, capture_output=True,
    )  # fmt: skip
    holder: dict[str, Endpoint] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            ep = holder["ep"]
            size = int(self.headers.get("Content-Length", "0"))
            ep.bodies.append(json.loads(self.rfile.read(size)))
            if ep.redirect:
                self.send_response(302)
                self.send_header("Location", ep.redirect)
            else:
                self.send_response(ep.status)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args) -> None:  # noqa: ANN002
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert, key)
    server.socket = server_context.wrap_socket(server.socket, server_side=True)
    client_context = ssl.create_default_context(cafile=str(cert))
    ep = holder["ep"] = Endpoint(server, client_context, cert)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield ep
    server.shutdown()
    server.server_close()


def _secret(folder: Path, url: str, mode: int = 0o600) -> Path:
    folder.mkdir(exist_ok=True)
    path = folder / slack.SECRET_NAME
    path.write_text(url + "\n")
    path.chmod(mode)
    return folder


def test_alert_arrives_once_per_window(endpoint: Endpoint, tmp_path: Path) -> None:
    folder = _secret(tmp_path / "config", endpoint.url)
    redis = fakeredis.FakeStrictRedis()
    alert = Alert("env_error_final", "o/r", 12)

    def send(a: Alert) -> str:
        return slack.send(a, redis, config_dir=folder, ssl_context=endpoint.client_context)

    assert send(alert) == "sent"
    assert send(Alert("env_error_final", "o/r", 13)) == "duplicate"  # 같은 원인의 다른 PR
    assert send(alert) == "duplicate"
    assert send(Alert("env_error_final", "o/other", 1)) == "sent"  # 다른 저장소
    assert send(Alert("pipeline_error", "o/r", 12)) == "sent"  # 다른 종류
    assert [b["text"] for b in endpoint.bodies] == [
        f"pr-proof 운영 알림 · env_error_final · o/r#12 · {TEXT['env_error_final']}",
        f"pr-proof 운영 알림 · env_error_final · o/other#1 · {TEXT['env_error_final']}",
        f"pr-proof 운영 알림 · pipeline_error · o/r#12 · {TEXT['pipeline_error']}",
    ]
    ttl = redis.ttl(alert.dedup_key)
    assert WINDOW_SECONDS - 5 <= ttl <= WINDOW_SECONDS == 30 * 60
    redis.delete(alert.dedup_key)  # 30분이 지나 표시가 사라지면 다시 온다
    assert send(alert) == "sent"
    assert len(endpoint.bodies) == 4


def test_window_boundary_follows_the_expiry(endpoint: Endpoint, tmp_path: Path) -> None:
    folder = _secret(tmp_path / "config", endpoint.url)
    redis = fakeredis.FakeStrictRedis()
    alert = Alert("worker_lost", "o/r", 3)
    kw = {"config_dir": folder, "ssl_context": endpoint.client_context, "window_seconds": 1}
    assert slack.send(alert, redis, **kw) == "sent"
    assert redis.pttl(alert.dedup_key) <= 1000
    assert slack.send(alert, redis, **kw) == "duplicate"


def test_failed_delivery_does_not_hold_the_window(endpoint: Endpoint, tmp_path: Path) -> None:
    folder = _secret(tmp_path / "config", endpoint.url)
    redis = fakeredis.FakeStrictRedis()
    alert = Alert("sync_failed", "o/r")
    endpoint.status = 500
    assert slack.send(alert, redis, config_dir=folder, ssl_context=endpoint.client_context) == (
        "failed"
    )
    assert redis.get(alert.dedup_key) is None
    endpoint.status = 200
    assert slack.send(alert, redis, config_dir=folder, ssl_context=endpoint.client_context) == (
        "sent"
    )


def test_redirect_is_not_followed(endpoint: Endpoint, tmp_path: Path) -> None:
    folder = _secret(tmp_path / "config", endpoint.url)
    endpoint.redirect = "http://127.0.0.1:1/elsewhere"
    redis = fakeredis.FakeStrictRedis()
    result = slack.send(Alert("tool_denied", "o/r", 1), redis, config_dir=folder,
                        ssl_context=endpoint.client_context)  # fmt: skip
    assert result == "failed" and len(endpoint.bodies) == 1


def test_certificate_is_checked(endpoint: Endpoint, tmp_path: Path) -> None:
    """믿지 않는 인증서의 엔드포인트에는 보내지 않는다 (기본 인증서 묶음으로 확인)."""
    folder = _secret(tmp_path / "config", endpoint.url)
    result = slack.send(Alert("preflight_failed"), fakeredis.FakeStrictRedis(), config_dir=folder)
    assert result == "failed" and endpoint.bodies == []


@pytest.mark.parametrize(
    "url",
    ["http://hooks.slack.com/services/T/B/x", "ftp://hooks.slack.com/x", "https:///nohost",
     "hooks.slack.com/services/T/B/x"],
)  # fmt: skip
def test_only_https_urls_are_used(url: str, tmp_path: Path, caplog) -> None:
    folder = _secret(tmp_path / "config", url)
    redis = fakeredis.FakeStrictRedis()
    assert slack.send(Alert("pipeline_error", "o/r", 1), redis, config_dir=folder) == "bad_url"
    assert redis.keys() == []
    assert url not in caplog.text


def test_no_secret_file_means_no_alerts(tmp_path: Path) -> None:
    redis = fakeredis.FakeStrictRedis()
    assert slack.send(Alert("pipeline_error"), redis, config_dir=tmp_path / "none") == "disabled"
    assert redis.keys() == []


def test_secret_file_readable_by_others_is_refused(tmp_path: Path) -> None:
    folder = _secret(tmp_path / "config", "https://hooks.slack.com/services/T/B/x", mode=0o644)
    assert slack.send(Alert("pipeline_error"), fakeredis.FakeStrictRedis(),
                      config_dir=folder) == "disabled"  # fmt: skip


def test_unreachable_redis_sends_nothing(endpoint: Endpoint, tmp_path: Path) -> None:
    folder = _secret(tmp_path / "config", endpoint.url)

    class DownRedis:
        def set(self, *a, **k):  # noqa: ANN002, ANN003
            raise RedisConnectionError("down")

    result = slack.send(Alert("pipeline_error", "o/r", 1), DownRedis(), config_dir=folder,
                        ssl_context=endpoint.client_context)  # type: ignore[arg-type]  # fmt: skip
    assert result == "unavailable" and endpoint.bodies == []


def test_message_carries_only_kind_repo_and_fixed_text() -> None:
    assert message(Alert("secret_blocked", "o/r", 7)) == (
        f"pr-proof 운영 알림 · secret_blocked · o/r#7 · {TEXT['secret_blocked']}"
    )
    assert message(Alert("preflight_failed")) == (
        f"pr-proof 운영 알림 · preflight_failed · {TEXT['preflight_failed']}"
    )
    # 저장소 이름에 쓸 수 없는 글자(멘션, 링크 꺾쇠)가 있으면 저장소를 넣지 않는다
    odd = Alert("sync_failed", "<!channel>/x|y", 1)
    assert "<" not in message(odd) and odd.dedup_key.endswith(":sync_failed:-")
    assert set(TEXT) == set(get_args(Kind))
