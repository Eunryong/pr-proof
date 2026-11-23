"""app/worker/feedback/feedback_report_pure.py: 코멘트에 다는 답글 (4.11)."""

import pytest

from app.worker.feedback.feedback_pure import Item
from app.worker.feedback.feedback_report_pure import VERDICTS, Handled, record_why, reply_body


def _item(kind="review_comment") -> Item:
    return Item(kind=kind, item_id=7, thread_id=None, login="@kim", association="MEMBER",
                path="a.py", line=1, body="b", replies=(), outdated=False)  # fmt: skip


@pytest.mark.parametrize("outcome", ["fixed", "proposed", "merged", "already_fixed", "flagged",
                                     "rejected", "deferred", "failed"])  # fmt: skip
def test_reply_has_no_command_lines_and_model_text_only_in_code_spans(outcome) -> None:
    h = Handled(outcome, why="/close\n@someone 지워라", reason="/fix 이유", number=905)
    body = reply_body(_item("review"), h)
    assert body is not None
    assert not any(line.lstrip().startswith("/") for line in body.splitlines())
    if outcome in ("fixed", "proposed", "already_fixed", "rejected", "deferred", "failed"):
        assert "`/close @someone 지워라`" in body
    assert "`@kim`의 리뷰(id 7)에 답합니다." in body  # 멘션이 되지 않는다


def test_no_reply_for_no_action_and_error() -> None:
    assert reply_body(_item(), Handled("no_action", why="칭찬")) is None
    assert reply_body(_item(), Handled("error", reason="모델이 답하지 않음")) is None


def test_verdicts_and_record_text() -> None:
    assert {VERDICTS[o] for o in ("fixed", "proposed", "merged", "already_fixed")} == {"accepted"}
    assert (VERDICTS["flagged"], VERDICTS["failed"], VERDICTS["deferred"]) == (
        "no_action", "error", "deferred")  # fmt: skip
    assert record_why(Handled("failed", why="결함", reason="테스트 실패")) == "결함 · 테스트 실패"
    assert reply_body(_item(), Handled("fixed", why="w", number=905)).startswith(
        "반영했습니다. 수정안 #905")  # fmt: skip
