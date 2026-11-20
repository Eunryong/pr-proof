"""Slack 운영 알림 보내기 (DESIGN.md 13절 6단계, v1 F12, 완료 기준 6-4, 위협 T33).

웹(동기화)과 워커가 같이 쓴다. Incoming Webhook URL은 비밀값 파일(`slack_webhook_url`)에서
`config.read_secret`으로만 읽는다. 파일이 없으면 알림을 보내지 않는다. https URL에만 보내고
넘김(redirect)은 따르지 않는다. 같은 알림은 Redis의 표시로 30분에 한 번만 보낸다. 보내기가
실패하면 표시를 지워 다음 오류가 다시 보내게 한다. 무엇이 잘못돼도 예외를 올리지 않는다(알림
때문에 작업이 멈추지 않는다). 로그에는 URL도 응답도 남기지 않는다.
"""

import json
import logging
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Final, Literal
from urllib.parse import urlsplit

from redis import Redis
from redis.exceptions import RedisError

from app import config
from app.models.ops_alert import WINDOW_SECONDS, Alert, message

SECRET_NAME: Final = "slack_webhook_url"
TIMEOUT_SECONDS: Final = 5

Outcome = Literal["sent", "duplicate", "disabled", "bad_url", "unavailable", "failed"]

log = logging.getLogger(__name__)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


def send(
    alert: Alert,
    redis: Redis,
    *,
    config_dir: Path | None = None,
    ssl_context: ssl.SSLContext | None = None,
    window_seconds: int = WINDOW_SECONDS,
) -> Outcome:
    try:
        url = config.read_secret(SECRET_NAME, config_dir)
    except config.ConfigError:
        return "disabled"
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        log.warning("운영 알림: URL이 https가 아니라 보내지 않음 (%s)", alert.kind)
        return "bad_url"
    key = alert.dedup_key
    try:
        if not redis.set(key, "1", nx=True, ex=window_seconds):
            return "duplicate"
    except RedisError:
        log.warning("운영 알림: Redis에 닿지 않아 보내지 않음 (%s)", alert.kind)
        return "unavailable"
    body = json.dumps({"text": message(alert)}, ensure_ascii=False).encode()
    request = urllib.request.Request(
        url, data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    opener = urllib.request.build_opener(
        _NoRedirect(), urllib.request.HTTPSHandler(context=ssl_context)
    )
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            ok = 200 <= response.status < 300
    except (OSError, ValueError) as e:  # URLError·HTTPError·시간 초과는 OSError다
        log.warning("운영 알림을 보내지 못함 (%s): %s", alert.kind, type(e).__name__)
        ok = False
    if ok:
        return "sent"
    try:
        redis.delete(key)
    except RedisError:
        pass
    return "failed"
