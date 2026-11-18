import pytest
from pydantic import ValidationError

from app.models.job import PRJob


def make(**overrides) -> PRJob:
    fields = {
        "repo": "owner/name",
        "pr_number": 7,
        "kind": "update",
        "head_sha": "abc123",
        "source": "webhook",
    }
    fields.update(overrides)
    return PRJob(**fields)


def test_update_key_includes_head_sha_and_kind():
    assert make().dedup_key == "owner/name#7#abc123#update"


def test_same_pr_different_head_has_different_key():
    assert make(head_sha="a").dedup_key != make(head_sha="b").dedup_key


def test_close_and_update_on_same_head_have_different_keys():
    assert make(kind="close").dedup_key != make(kind="update").dedup_key


def test_command_key_uses_comment_id_not_head_sha():
    first = make(kind="command", command="/recheck", comment_id=1, head_sha=None)
    second = make(kind="command", command="/recheck", comment_id=2, head_sha=None)
    assert first.dedup_key == "owner/name#7#command#1"
    assert first.dedup_key != second.dedup_key


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "command", "command": "/recheck"},
        {"kind": "command", "comment_id": 1},
        {"kind": "update", "command": "/recheck"},
        {"kind": "update", "comment_id": 1},
        {"kind": "merge"},
        {"source": "manual"},
    ],
)
def test_invalid_jobs_are_rejected(fields):
    with pytest.raises(ValidationError):
        make(**fields)


def test_round_trips_through_json():
    job = make(kind="command", command="/fix lint", comment_id=9, delivery_id="d-1")
    assert PRJob.model_validate_json(job.model_dump_json()) == job
