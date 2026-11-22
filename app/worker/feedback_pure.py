"""PR 코멘트에서 처리할 항목을 고르는 규칙 (docs/DESIGN.md "PR 코멘트"). 입출력이 없다.

GitHub에서 읽은 스레드·리뷰·일반 코멘트를 `Item`으로 바꾼다. 다룰 글인지는 웹훅·동기화와 같은
`feedback_in_comment()`로 정한다. 줄 코멘트는 스레드 하나가 항목 하나이고 스레드를 연 첫 코멘트가
기준이다.
"""

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Protocol

from app.shared.feedback_pure import PIPELINE_MARKER_PREFIX, FeedbackKind, feedback_in_comment


class Written(Protocol):
    """글쓴이와 본문이 있는 코멘트. `github.ThreadComment`·`ReviewBody`·`PullComment`가 맞는다."""

    @property
    def id(self) -> int: ...
    @property
    def login(self) -> str: ...
    @property
    def association(self) -> str: ...
    @property
    def body(self) -> str: ...


class Thread(Protocol):
    """줄 리뷰 코멘트의 스레드. `github.ReviewThread`가 맞는다."""

    @property
    def id(self) -> str: ...
    @property
    def resolved(self) -> bool: ...
    @property
    def outdated(self) -> bool: ...
    @property
    def path(self) -> str: ...
    @property
    def line(self) -> int | None: ...
    @property
    def comments(self) -> Sequence[Written]: ...


@dataclass(frozen=True)
class Item:
    """처리할 코멘트 하나. 본문과 답글은 사람이나 Copilot이 쓴 글이라 믿지 않는 자료다."""

    kind: FeedbackKind
    item_id: int  # 줄 코멘트는 스레드의 첫 코멘트 id, 리뷰는 리뷰 id, 일반 코멘트는 코멘트 id
    thread_id: str | None  # 줄 코멘트만
    login: str
    association: str
    path: str  # 줄 코멘트만. 아니면 ""
    line: int | None
    body: str
    replies: tuple[str, ...]  # 스레드에서 뒤따른 답글 중 파이프라인 표시가 없는 것
    outdated: bool


def thread_item(thread: Thread) -> Item | None:
    """스레드 하나의 항목. 이미 해결된 스레드와 다룰 글이 아닌 스레드는 None."""
    if thread.resolved or not thread.comments:
        return None
    first, *rest = thread.comments
    if not feedback_in_comment(first.body, first.association, first.login)[0]:
        return None
    return Item(
        kind="review_comment",
        item_id=first.id,
        thread_id=thread.id,
        login=first.login,
        association=first.association,
        path=thread.path,
        line=thread.line,
        body=first.body,
        replies=tuple(c.body for c in rest if PIPELINE_MARKER_PREFIX not in c.body),
        outdated=thread.outdated,
    )


def flat_item(kind: FeedbackKind, written: Written) -> Item | None:
    """리뷰 본문이나 일반 코멘트의 항목. 다룰 글이 아니면 None. 둘에는 스레드가 없다."""
    if not feedback_in_comment(written.body, written.association, written.login)[0]:
        return None
    return Item(
        kind=kind,
        item_id=written.id,
        thread_id=None,
        login=written.login,
        association=written.association,
        path="",
        line=None,
        body=written.body,
        replies=(),
        outdated=False,
    )


def unhandled(items: Sequence[Item | None], handled: Collection[tuple[str, int]]) -> list[Item]:
    """항목이 아닌 것(None)과 이미 처리한 것((종류, id))을 뺀다. 순서는 그대로다."""
    return [item for item in items if item is not None and (item.kind, item.item_id) not in handled]
