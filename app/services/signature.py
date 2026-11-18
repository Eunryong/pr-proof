"""GitHub 웹훅 서명 검증 (X-Hub-Signature-256)."""

import hashlib
import hmac
from typing import Final

PREFIX: Final = "sha256="


def verify(secret: str, body: bytes, header: str | None) -> bool:
    if not header or not header.startswith(PREFIX):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix(PREFIX))


def sign(secret: str, body: bytes) -> str:
    """테스트와 수동 재전송용 서명."""
    return PREFIX + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
