import copy
import json
from pathlib import Path
from typing import Final

import fakeredis
import pytest
from rq import Queue

from app.api import signature, webhook
from app.api.webhook import Ignored, parse_event
from app.shared import state_keys_pure
from app.shared.comment_pure import COMMENT_MARKER, REPLY_MARKER
from app.shared.feedback_pure import FEEDBACK_MARKER
from app.shared.job_pure import PRJob

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


def test_author_values_are_carried_for_the_record(pr_opened: dict, comment_created: dict) -> None:
    """api는 판단하지 않고 실어 넘기기만 한다 (8.6). 워커는 이 값으로 정하지 않는다."""
    pr_opened["pull_request"]["author_association"] = "NONE"
    job = parse_event("pull_request", pr_opened, "d-1", ALLOWED)
    assert (job.author_association, job.author_login) == ("NONE", "Eunryong")
    comment_created["issue"] |= {"author_association": "CONTRIBUTOR", "user": {"login": "x"}}
    job = parse_event("issue_comment", comment_created, "d-2", ALLOWED)
    assert (job.author_association, job.author_login) == ("CONTRIBUTOR", "x")
    assert (job.commenter_association, job.commenter_login) == ("OWNER", "Eunryong")


def test_untrusted_authors_pr_is_queued_like_any_other(pr_opened: dict) -> None:
    pr_opened["pull_request"]["author_association"] = "FIRST_TIMER"
    assert parse_event("pull_request", pr_opened, "d-1", ALLOWED).kind == "update"


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
        (lambda p: p["comment"].update(body="좋아요", author_association="CONTRIBUTOR"),
         "코멘트 권한 없음"),
        (lambda p: p["comment"].update(author_association="CONTRIBUTOR"), "권한 없음"),
        (lambda p: p["comment"].update(author_association="NONE"), "권한 없음"),
        (lambda p: p["comment"].update(body="/fix model", author_association="CONTRIBUTOR"),
         "권한 없음"),
        (lambda p: p["comment"].update(body="/fix", author_association="FIRST_TIMER"), "권한 없음"),
    ],
    ids=["edited", "issue", "own-comment", "plain-contributor", "contributor", "none",
         "fix-model-contributor", "fix-first-timer"],
)  # fmt: skip
def test_ignored_comment_cases(comment_created: dict, change, reason: str) -> None:
    change(comment_created)
    assert reason in _ignored("issue_comment", comment_created)


# --- PR 코멘트 (feedback) · 수정안 머지 (resolve) -------------------------------------


def _review_payload(event: str, pr_opened: dict, **text) -> dict:
    """pull_request_review(submitted)와 pull_request_review_comment(created) 이벤트."""
    key = "review" if event == "pull_request_review" else "comment"
    fields = {"id": 77, "body": "이 줄이 위험합니다.", "author_association": "NONE",
              "user": {"login": "copilot-pull-request-reviewer[bot]"}} | text  # fmt: skip
    return {
        "action": "submitted" if key == "review" else "created",
        key: fields,
        "pull_request": pr_opened["pull_request"],
        "repository": pr_opened["repository"],
    }


REVIEW_EVENTS: Final = ("pull_request_review", "pull_request_review_comment")


@pytest.mark.parametrize("event", REVIEW_EVENTS)
def test_copilot_review_makes_a_feedback_job(pr_opened: dict, event: str) -> None:
    job = parse_event(event, _review_payload(event, pr_opened), "d-3", ALLOWED)
    assert (job.kind, job.pr_number, job.source, job.delivery_id) == (
        "feedback", 1, "webhook", "d-3",
    )  # fmt: skip
    assert job.head_sha == "1" * 40 and job.base_sha == "2" * 40
    assert job.command is None and job.comment_id is None


@pytest.mark.parametrize("event", REVIEW_EVENTS)
@pytest.mark.parametrize("assoc", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_trusted_human_review_makes_a_feedback_job(pr_opened: dict, event: str, assoc: str) -> None:
    payload = _review_payload(event, pr_opened, author_association=assoc, user={"login": "kim"})
    assert parse_event(event, payload, "d-3", ALLOWED).kind == "feedback"


@pytest.mark.parametrize("event", REVIEW_EVENTS)
@pytest.mark.parametrize("assoc", ["CONTRIBUTOR", "NONE", "FIRST_TIMER"])
def test_untrusted_human_review_is_ignored(pr_opened: dict, event: str, assoc: str) -> None:
    payload = _review_payload(event, pr_opened, author_association=assoc, user={"login": "x"})
    assert "권한" in _ignored(event, payload)


@pytest.mark.parametrize("event", REVIEW_EVENTS)
@pytest.mark.parametrize(
    "body", [FEEDBACK_MARKER + "\n답", COMMENT_MARKER + "\n결과", REPLY_MARKER + "\n> /fix"]
)
def test_pipelines_own_reply_does_not_make_a_feedback_job(
    pr_opened: dict, event: str, body: str
) -> None:
    """파이프라인은 주인의 계정으로 단다. 답글이 다시 작업을 만들면 끝없이 돈다."""
    payload = _review_payload(event, pr_opened, body=body, author_association="OWNER",
                              user={"login": "owner"})  # fmt: skip
    assert "자신의 코멘트" in _ignored(event, payload)


@pytest.mark.parametrize("event", REVIEW_EVENTS)
def test_review_ignored_cases(pr_opened: dict, event: str) -> None:
    empty = _review_payload(event, pr_opened, body=None)  # 줄 코멘트만 단 리뷰는 본문이 null
    assert "빈" in _ignored(event, empty)

    command = _review_payload(event, pr_opened, body="/fix model")
    assert "명령" in _ignored(event, command)

    edited = _review_payload(event, pr_opened)
    edited["action"] = "edited"
    assert f"{event}.edited" in _ignored(event, edited)

    proposal = _review_payload(event, pr_opened)
    proposal["pull_request"]["head"]["ref"] = "pipeline/pr-1-fix-model"
    assert "제안 PR" in _ignored(event, proposal)


def test_issue_comment_review_text_makes_a_feedback_job_without_head(
    comment_created: dict,
) -> None:
    comment_created["comment"] |= {
        "body": "이 PR은 설명이 부족합니다",
        "author_association": "OWNER",
    }
    job = parse_event("issue_comment", comment_created, "d-4", ALLOWED)
    assert (job.kind, job.pr_number, job.head_sha, job.base_sha) == ("feedback", 1, None, None)


def test_issue_comment_command_stays_a_command(comment_created: dict) -> None:
    assert parse_event("issue_comment", comment_created, "d-4", ALLOWED).kind == "command"


@pytest.mark.parametrize(
    "body", [FEEDBACK_MARKER + "\n답", COMMENT_MARKER + "\n결과", REPLY_MARKER + "\n> /recheck"]
)
def test_pipelines_issue_comments_make_no_job(comment_created: dict, body: str) -> None:
    """결과 코멘트·명령 답글·코멘트 답글은 모두 주인의 계정으로 달려 OWNER 글처럼 보인다."""
    comment_created["comment"]["body"] = body
    assert "자신의 코멘트" in _ignored("issue_comment", comment_created)


def test_issue_comment_from_untrusted_author_is_ignored(comment_created: dict) -> None:
    comment_created["comment"] |= {"body": "좋아요", "author_association": "NONE"}
    assert "권한" in _ignored("issue_comment", comment_created)
    comment_created["comment"]["user"] = {"login": "Copilot"}
    assert parse_event("issue_comment", comment_created, "d-4", ALLOWED).kind == "feedback"


def _fix_pr_closed(pr_opened: dict, *, merged: bool, ref: str = "pipeline/pr-5-fix-model") -> dict:
    payload = copy.deepcopy(pr_opened)
    payload["action"] = "closed"
    payload["pull_request"] |= {"merged": merged, "number": 901}
    payload["pull_request"]["head"]["ref"] = ref
    return payload


def test_merged_fix_pr_makes_a_resolve_job_for_the_original_pr(pr_opened: dict) -> None:
    job = parse_event("pull_request", _fix_pr_closed(pr_opened, merged=True), "d-5", ALLOWED)
    assert (job.kind, job.pr_number, job.source, job.delivery_id) == (
        "resolve",
        5,
        "webhook",
        "d-5",
    )
    assert job.head_sha is None


def test_fix_pr_closed_without_merge_makes_no_job(pr_opened: dict) -> None:
    assert "제안 PR" in _ignored("pull_request", _fix_pr_closed(pr_opened, merged=False))


def test_other_pipeline_pr_events_stay_ignored(pr_opened: dict) -> None:
    opened = _fix_pr_closed(pr_opened, merged=True) | {"action": "opened"}
    assert "제안 PR" in _ignored("pull_request", opened)
    other_shape = _fix_pr_closed(pr_opened, merged=True, ref="pipeline/experiment")
    assert "제안 PR" in _ignored("pull_request", other_shape)
    no_merged_field = _fix_pr_closed(pr_opened, merged=True)
    del no_merged_field["pull_request"]["merged"]
    assert "제안 PR" in _ignored("pull_request", no_merged_field)


def test_merged_normal_pr_is_still_a_close_job(pr_opened: dict) -> None:
    pr_opened["action"] = "closed"
    pr_opened["pull_request"]["merged"] = True
    assert parse_event("pull_request", pr_opened, "d-5", ALLOWED).kind == "close"


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
    assert len(Queue(state_keys_pure.QUEUE_NAME, connection=redis)) == 1


def test_bad_signature_is_401_and_not_recorded(redis, pr_opened: dict) -> None:
    assert _handle(redis, pr_opened, sig="sha256=bad").status_code == 401
    assert _handle(redis, pr_opened).body["status"] == "queued"  # 같은 delivery라도 처리됨


def test_redelivered_webhook_is_skipped(redis, pr_opened: dict) -> None:
    _handle(redis, pr_opened)
    assert _handle(redis, pr_opened).body["status"] == "duplicate_delivery"
    assert len(Queue(state_keys_pure.QUEUE_NAME, connection=redis)) == 1


def test_ignored_event_is_202(redis, pr_opened: dict) -> None:
    pr_opened["action"] = "labeled"
    response = _handle(redis, pr_opened)
    assert response.status_code == 202 and response.body["status"] == "ignored"


def test_allowlist_ignores_case_and_job_uses_the_configured_name(pr_opened) -> None:
    """GitHub의 이름은 대소문자를 가리지 않는다. 설정에 다른 대소문자로 적혀 있어도 같은 저장소로
    받고, 작업에는 설정에 적힌 이름을 쓴다(동기화가 만드는 작업과 같은 이름)."""
    job = parse_event("pull_request", pr_opened, "d-1", frozenset({"eunryong/EVAL"}))
    assert isinstance(job, PRJob) and job.repo == "eunryong/EVAL"
    other = parse_event("pull_request", pr_opened, "d-1", frozenset({"eunryong/evaluate"}))
    assert isinstance(other, Ignored) and "허용 목록에 없는 리포: Eunryong/eval" in other.reason
