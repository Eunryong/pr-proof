"""웹훅 한 건: 서명 검증 → 이벤트 해석 → 중복 제거 → 큐 등록 (docs/DESIGN.md F1)."""

from dataclasses import dataclass
from typing import Any, Final

from redis import Redis

from app.api import queue, responses, signature
from app.shared.comment_pure import command_in_comment
from app.shared.job_pure import PRJob

UPDATE_ACTIONS: Final = frozenset({"opened", "reopened", "synchronize", "ready_for_review"})
PIPELINE_BRANCH_PREFIX: Final = "pipeline/"


@dataclass(frozen=True)
class Ignored:
    reason: str


def parse_event(
    event: str, payload: dict[str, Any], delivery_id: str | None, allowed_repos: frozenset[str]
) -> PRJob | Ignored:
    """GitHub 이벤트를 작업으로 바꾼다. 처리하지 않을 이벤트는 Ignored와 이유를 돌려준다."""
    repo = (payload.get("repository") or {}).get("full_name")
    if repo not in allowed_repos:
        return Ignored(f"허용 목록에 없는 리포: {repo}")
    action = payload.get("action")

    if event == "pull_request":
        pr = payload["pull_request"]
        if pr["head"]["ref"].startswith(PIPELINE_BRANCH_PREFIX):
            return Ignored("파이프라인 제안 PR")
        if action in UPDATE_ACTIONS:
            kind = "update"
        elif action == "closed":
            kind = "close"
        else:
            return Ignored(f"처리하지 않는 동작: pull_request.{action}")
        return PRJob(
            repo=repo,
            pr_number=pr["number"],
            kind=kind,
            head_sha=pr["head"]["sha"],
            base_sha=pr["base"]["sha"],
            source="webhook",
            delivery_id=delivery_id,
            author_association=pr.get("author_association"),
            author_login=(pr.get("user") or {}).get("login"),
        )

    if event == "issue_comment":
        comment, issue = payload["comment"], payload["issue"]
        if action != "created":
            return Ignored(f"처리하지 않는 동작: issue_comment.{action}")
        if "pull_request" not in issue:
            return Ignored("PR이 아닌 이슈의 코멘트")
        command, reason = command_in_comment(comment.get("body"), comment.get("author_association"))
        if command is None:
            return Ignored(reason)
        return PRJob(
            repo=repo,
            pr_number=issue["number"],
            kind="command",
            command=command,
            comment_id=comment["id"],
            source="webhook",
            delivery_id=delivery_id,
            author_association=issue.get("author_association"),
            author_login=(issue.get("user") or {}).get("login"),
            commenter_association=comment.get("author_association"),
            commenter_login=(comment.get("user") or {}).get("login"),
        )

    return Ignored(f"처리하지 않는 이벤트: {event}")


def handle(
    *,
    body: bytes,
    payload: dict[str, Any],
    event: str | None,
    delivery_id: str | None,
    signature_header: str | None,
    secret: str,
    allowed_repos: frozenset[str],
    redis: Redis,
) -> responses.WebhookResponse:
    if not signature.verify(secret, body, signature_header):
        return responses.unauthorized()
    if delivery_id and queue.seen_delivery(redis, delivery_id):
        return responses.duplicate_delivery()
    parsed = parse_event(event or "", payload, delivery_id, allowed_repos)
    if isinstance(parsed, Ignored):
        return responses.ignored(parsed.reason)
    return responses.accepted(parsed, queue.enqueue(redis, parsed))
