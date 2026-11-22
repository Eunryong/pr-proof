from dataclasses import dataclass, field

import pytest

from app.shared.feedback_pure import FEEDBACK_MARKER
from app.worker.feedback_pure import Item, flat_item, thread_item, unhandled


@dataclass(frozen=True)
class C:
    id: int
    login: str = "copilot-pull-request-reviewer"
    association: str = "NONE"
    body: str = "이 줄은 위험합니다."
    original_commit: str = ""


@dataclass(frozen=True)
class T:
    comments: tuple[C, ...] = field(default_factory=lambda: (C(10),))
    id: str = "PRRT_1"
    resolved: bool = False
    outdated: bool = False
    path: str = "src/a.py"
    line: int | None = 12
    original_line: int | None = None


def test_thread_is_one_item_based_on_the_first_comment() -> None:
    thread = T(
        comments=(C(10), C(11, "kim", "MEMBER", "저도 동의합니다"), C(12, "kim", "MEMBER", "ㅇ"))
    )
    assert thread_item(thread) == Item(
        kind="review_comment", item_id=10, thread_id="PRRT_1",
        login="copilot-pull-request-reviewer", association="NONE", path="src/a.py", line=12,
        body="이 줄은 위험합니다.", replies=("저도 동의합니다", "ㅇ"), outdated=False,
    )  # fmt: skip


def test_replies_exclude_the_pipelines_own() -> None:
    mine = C(11, "owner", "OWNER", f"{FEEDBACK_MARKER}\n받아들였습니다")
    human = C(12, "kim", "MEMBER", "한 줄 더")
    assert thread_item(T(comments=(C(10), mine, human))).replies == ("한 줄 더",)


def test_resolved_thread_is_dropped() -> None:
    assert thread_item(T(resolved=True)) is None


def test_outdated_and_lost_line_are_carried() -> None:
    item = thread_item(T(outdated=True, line=None))
    assert item.outdated is True and item.line is None


def test_empty_thread_is_dropped() -> None:
    assert thread_item(T(comments=())) is None


@pytest.mark.parametrize(
    "first",
    [
        C(10, "outsider", "CONTRIBUTOR"),
        C(10, "outsider", "NONE"),
        C(10, "owner", "OWNER", f"{FEEDBACK_MARKER}\n답"),
        C(10, "owner", "OWNER", "<!-- pr-proof -->\n결과"),
        C(10, "owner", "OWNER", "/fix model"),
        C(10, "owner", "OWNER", ""),
    ],
    ids=["contributor", "none", "own-reply", "own-result", "command", "empty"],
)
def test_thread_started_by_something_not_handled_is_dropped(first: C) -> None:
    """스레드는 첫 코멘트로 판단한다. 뒤에 믿을 만한 사람이 답해도 연 글이 아니면 다루지 않는다."""
    assert thread_item(T(comments=(first, C(11, "kim", "MEMBER", "좋은 지적")))) is None


def test_thread_started_by_a_trusted_human() -> None:
    assert thread_item(T(comments=(C(10, "kim", "COLLABORATOR"),))).login == "kim"


def test_flat_items_have_no_thread_or_place() -> None:
    review = flat_item("review", C(5, "kim", "MEMBER", "전체적으로 테스트가 부족합니다"))
    assert review == Item(
        kind="review", item_id=5, thread_id=None, login="kim", association="MEMBER", path="",
        line=None, body="전체적으로 테스트가 부족합니다", replies=(), outdated=False,
    )  # fmt: skip
    assert flat_item("issue_comment", C(6, "Copilot", "NONE")).kind == "issue_comment"


@pytest.mark.parametrize(
    "written",
    [C(5, "x", "NONE"), C(5, "x", "FIRST_TIMER"), C(5, "o", "OWNER", f"{FEEDBACK_MARKER}\n답"),
     C(5, "o", "OWNER", "/recheck"), C(5, "o", "OWNER", " ")],
)  # fmt: skip
def test_flat_item_filters_like_the_webhook(written: C) -> None:
    assert flat_item("issue_comment", written) is None


def test_unhandled_removes_none_and_handled_in_order() -> None:
    a = flat_item("review", C(1, "o", "OWNER"))
    b = flat_item("issue_comment", C(1, "o", "OWNER"))  # 종류가 다르면 다른 글이다
    c = thread_item(T())
    result = unhandled([a, None, b, c], {("review", 1)})
    assert [(i.kind, i.item_id) for i in result] == [("issue_comment", 1), ("review_comment", 10)]
    assert unhandled([a, b], {("review", 1), ("issue_comment", 1)}) == []
