"""웹훅 한 건: 서명 검증 → 이벤트 해석 → 중복 제거 → 큐 등록 (docs/DESIGN.md F1)."""

from dataclasses import dataclass
from typing import Any, Final

from redis import Redis

from app.api import queue, responses, signature
from app.shared.comment_pure import command_in_comment
from app.shared.feedback_pure import feedback_in_comment, fix_branch_pr
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
    sent = (payload.get("repository") or {}).get("full_name")
    # GitHub의 계정·저장소 이름은 대소문자를 가리지 않는다. 설정에 다른 대소문자로 적혀 있어도 같은
    # 저장소이고, 작업에는 설정에 적힌 철자를 쓴다(동기화가 만드는 작업과 같은 이름이 되게)
    repo = next(
        (r for r in allowed_repos if isinstance(sent, str) and r.casefold() == sent.casefold()),
        None,
    )
    if repo is None:
        return Ignored(f"허용 목록에 없는 리포: {sent}")
    action = payload.get("action")

    if event == "pull_request":
        pr = payload["pull_request"]
        ref = pr["head"]["ref"]
        if ref.startswith(PIPELINE_BRANCH_PREFIX):
            # 머지된 수정안만 원래 PR의 스레드를 해결하는 작업이 된다. 해결은 워커가 한다
            original = fix_branch_pr(ref)
            if action == "closed" and pr.get("merged") and original is not None:
                return PRJob(
                    repo=repo, pr_number=original, kind="resolve", source="webhook",
                    delivery_id=delivery_id,
                )  # fmt: skip
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
            return _feedback_job(
                repo,
                issue["number"],
                comment,
                None,
                None,
                delivery_id,
                reason,
                author=issue,
            )
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

    if event in ("pull_request_review", "pull_request_review_comment"):
        wanted = "submitted" if event == "pull_request_review" else "created"
        if action != wanted:
            return Ignored(f"처리하지 않는 동작: {event}.{action}")
        pr = payload["pull_request"]
        if pr["head"]["ref"].startswith(PIPELINE_BRANCH_PREFIX):
            return Ignored("파이프라인 제안 PR")
        text = payload["review"] if event == "pull_request_review" else payload["comment"]
        return _feedback_job(
            repo,
            pr["number"],
            text,
            pr["head"]["sha"],
            pr["base"]["sha"],
            delivery_id,
            "",
            author=pr,
        )

    return Ignored(f"처리하지 않는 이벤트: {event}")


def _feedback_job(
    repo: str,
    pr_number: int,
    text: dict[str, Any],
    head_sha: str | None,
    base_sha: str | None,
    delivery_id: str | None,
    command_reason: str,
    *,
    author: dict[str, Any],
) -> PRJob | Ignored:
    """다룰 코멘트면 feedback 작업, 아니면 Ignored. 어떤 코멘트인지는 싣지 않는다(워커가 읽는다).

    issue_comment는 먼저 명령인지 본다. 명령(`/`로 시작하는 글)은 여기서 받지 않고, 무시 이유는
    명령 쪽의 이유(command_reason, 예: 권한 없음)를 그대로 쓴다.
    """
    login = (text.get("user") or {}).get("login")
    ok, reason = feedback_in_comment(text.get("body"), text.get("author_association"), login)
    if not ok:
        return Ignored(command_reason if reason == "명령" and command_reason else reason)
    return PRJob(
        repo=repo,
        pr_number=pr_number,
        kind="feedback",
        head_sha=head_sha,
        base_sha=base_sha,
        source="webhook",
        delivery_id=delivery_id,
        author_association=author.get("author_association"),
        author_login=(author.get("user") or {}).get("login"),
    )


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
