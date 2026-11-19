import copy
import json
from pathlib import Path
from typing import Final

import fakeredis
import pytest
from rq import Queue

from app.controllers import webhook
from app.controllers.webhook import Ignored, parse_event
from app.models import state_keys
from app.models.comment import COMMENT_MARKER
from app.services import signature

FIXTURES: Final = Path(__file__).resolve().parent.parent / "fixtures" / "webhooks"
ALLOWED: Final = frozenset({"Eunryong/eval"})
SECRET: Final = "test-secret"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture
def pr_opened() -> dict:
    return _load("pull_request_opened")


@pytest.fixture
def comment_created() -> dict:
    return _load("issue_comment_created")


# --- parse_event --------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "kind"),
    [("opened", "update"), ("reopened", "update"), ("synchronize", "update"),
     ("ready_for_review", "update"), ("closed", "close")],
)  # fmt: skip
def test_pull_request_actions(pr_opened: dict, action: str, kind: str) -> None:
    pr_opened["action"] = action
    job = parse_event("pull_request", pr_opened, "d-1", ALLOWED)
    assert job.kind == kind
    assert (job.repo, job.pr_number, job.source, job.delivery_id) == (
        "Eunryong/eval",
        1,
        "webhook",
        "d-1",
    )
    assert job.head_sha == "1" * 40 and job.base_sha == "2" * 40


def _ignored(event: str, payload: dict) -> str:
    result = parse_event(event, payload, "d-1", ALLOWED)
    assert isinstance(result, Ignored)
    return result.reason


def test_ignored_pull_request_cases(pr_opened: dict) -> None:
    edited = copy.deepcopy(pr_opened)
    edited["action"] = "edited"
    assert "pull_request.edited" in _ignored("pull_request", edited)

    other = copy.deepcopy(pr_opened)
    other["repository"]["full_name"] = "someone/else"
    assert "허용 목록" in _ignored("pull_request", other)

    proposal = copy.deepcopy(pr_opened)
    proposal["pull_request"]["head"]["ref"] = "pipeline/pr-1-fix-lint"
    assert "제안 PR" in _ignored("pull_request", proposal)

    assert "이벤트" in _ignored("push", pr_opened)


def test_command_comment(comment_created: dict) -> None:
    job = parse_event("issue_comment", comment_created, "d-2", ALLOWED)
    assert (job.kind, job.command, job.comment_id, job.pr_number) == (
        "command",
        "/recheck",
        9001,
        1,
    )


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda p: p.update(action="edited"), "issue_comment.edited"),
        (lambda p: p["issue"].pop("pull_request"), "PR이 아닌"),
        (lambda p: p["comment"].update(body=f"{COMMENT_MARKER}\n/recheck"), "자신의 코멘트"),
        (lambda p: p["comment"].update(body="좋아요"), "명령이 아닌"),
        (lambda p: p["comment"].update(author_association="CONTRIBUTOR"), "권한 없음"),
        (lambda p: p["comment"].update(author_association="NONE"), "권한 없음"),
        (lambda p: p["comment"].update(body="/fix model", author_association="CONTRIBUTOR"),
         "권한 없음"),
        (lambda p: p["comment"].update(body="/fix", author_association="FIRST_TIMER"), "권한 없음"),
    ],
    ids=["edited", "issue", "own-comment", "not-command", "contributor", "none",
         "fix-model-contributor", "fix-first-timer"],
)  # fmt: skip
def test_ignored_comment_cases(comment_created: dict, change, reason: str) -> None:
    change(comment_created)
    assert reason in _ignored("issue_comment", comment_created)


# --- handle -------------------------------------------------------------------


@pytest.fixture
def redis():
    return fakeredis.FakeStrictRedis()


def _handle(redis, payload: dict, *, event: str = "pull_request", delivery: str = "d-1", sig=None):
    body = json.dumps(payload).encode()
    return webhook.handle(
        body=body,
        payload=payload,
        event=event,
        delivery_id=delivery,
        signature_header=sig if sig is not None else signature.sign(SECRET, body),
        secret=SECRET,
        allowed_repos=ALLOWED,
        redis=redis,
    )


def test_valid_webhook_is_queued(redis, pr_opened: dict) -> None:
    response = _handle(redis, pr_opened)
    assert response.status_code == 202 and response.body["status"] == "queued"
    assert len(Queue(state_keys.QUEUE_NAME, connection=redis)) == 1


def test_bad_signature_is_401_and_not_recorded(redis, pr_opened: dict) -> None:
    assert _handle(redis, pr_opened, sig="sha256=bad").status_code == 401
    assert _handle(redis, pr_opened).body["status"] == "queued"  # 같은 delivery라도 처리됨


def test_redelivered_webhook_is_skipped(redis, pr_opened: dict) -> None:
    _handle(redis, pr_opened)
    assert _handle(redis, pr_opened).body["status"] == "duplicate_delivery"
    assert len(Queue(state_keys.QUEUE_NAME, connection=redis)) == 1


def test_ignored_event_is_202(redis, pr_opened: dict) -> None:
    pr_opened["action"] = "labeled"
    response = _handle(redis, pr_opened)
    assert response.status_code == 202 and response.body["status"] == "ignored"
