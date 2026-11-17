"""웹훅 응답. GitHub에는 처리 결과와 무관하게 빨리 응답한다 (서명 실패만 401)."""

from dataclasses import dataclass, field
from typing import Any

from app.models.job import PRJob


@dataclass(frozen=True)
class WebhookResponse:
    status_code: int
    body: dict[str, Any] = field(default_factory=dict)


def unauthorized() -> WebhookResponse:
    return WebhookResponse(401, {"status": "invalid_signature"})


def duplicate_delivery() -> WebhookResponse:
    return WebhookResponse(202, {"status": "duplicate_delivery"})


def ignored(reason: str) -> WebhookResponse:
    return WebhookResponse(202, {"status": "ignored", "reason": reason})


def accepted(job: PRJob, result: str) -> WebhookResponse:
    return WebhookResponse(202, {"status": result, "job": job.dedup_key})
