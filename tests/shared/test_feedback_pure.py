import pytest

from app.shared.comment_pure import COMMENT_MARKER, REPLY_MARKER
from app.shared.feedback_pure import (
    COPILOT_LOGINS,
    FEEDBACK_MARKER,
    PIPELINE_MARKER_PREFIX,
    feedback_in_comment,
    fix_branch_pr,
)


@pytest.mark.parametrize("assoc", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_trusted_authors_are_handled(assoc: str) -> None:
    assert feedback_in_comment("이 줄을 고쳐 주세요", assoc, "someone") == (True, "")


@pytest.mark.parametrize(
    "assoc", ["CONTRIBUTOR", "NONE", "FIRST_TIMER", "FIRST_TIME_CONTRIBUTOR", None]
)
def test_untrusted_authors_are_not(assoc: str | None) -> None:
    ok, reason = feedback_in_comment("고쳐 주세요", assoc, "someone")
    assert not ok and "권한" in reason


@pytest.mark.parametrize("login", sorted(COPILOT_LOGINS))
@pytest.mark.parametrize("assoc", ["NONE", "CONTRIBUTOR", None])
def test_copilot_is_handled_whatever_the_association(login: str, assoc: str | None) -> None:
    assert feedback_in_comment("This can overflow.", assoc, login) == (True, "")


@pytest.mark.parametrize(
    "marker", [COMMENT_MARKER, REPLY_MARKER, FEEDBACK_MARKER, PIPELINE_MARKER_PREFIX]
)
def test_the_pipelines_own_comments_are_not(marker: str) -> None:
    """파이프라인은 주인의 계정으로 글을 단다. 계정(OWNER)이 아니라 표시로만 가린다."""
    for body in (f"{marker}\n본문", f"앞 글\n{marker}"):
        assert feedback_in_comment(body, "OWNER", "me") == (False, "파이프라인 자신의 코멘트")
    assert feedback_in_comment(f"{marker}\n본문", "NONE", "Copilot")[0] is False


@pytest.mark.parametrize("body", ["/recheck", "/fix model", "  /fix\n설명"])
def test_commands_are_left_to_command_in_comment(body: str) -> None:
    assert feedback_in_comment(body, "OWNER", "me") == (False, "명령")


@pytest.mark.parametrize("body", [None, "", "  \n "])
def test_empty_body_is_not(body: str | None) -> None:
    assert feedback_in_comment(body, "OWNER", "me")[0] is False


def test_markers_all_start_with_the_same_prefix() -> None:
    assert all(
        m.startswith(PIPELINE_MARKER_PREFIX)
        for m in (COMMENT_MARKER, REPLY_MARKER, FEEDBACK_MARKER)
    )


@pytest.mark.parametrize(
    ("ref", "number"),
    [
        ("pipeline/pr-7-fix-mechanical", 7),
        ("pipeline/pr-123-fix-model", 123),
        ("pipeline/pr-7-fix-", None),
        ("pipeline/pr-x-fix-model", None),
        ("pipeline/pr-7", None),
        ("feature/pr-7-fix-model", None),
        ("pipeline/pr-7-fix-a/b", None),
        ("", None),
    ],
)
def test_fix_branch_pr(ref: str, number: int | None) -> None:
    assert fix_branch_pr(ref) == number


def test_deferred_error_and_superseded_are_revisited_only_on_a_new_head() -> None:
    from app.shared.feedback_pure import handled

    assert handled("rejected", "old", "new")
    assert handled("accepted", "old", "new")
    assert not handled("accepted", "old", "new", superseded=True)
    assert not handled("deferred", "old", "new") and handled("deferred", "new", "new")
    assert not handled("error", "old", "new") and handled("error", "new", "new")
