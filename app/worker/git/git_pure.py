"""git과 GitHub에서 읽은 값의 데이터와 읽기 (docs/DESIGN.md 4.3, 4.5 끝). 계산만 한다.

base 커밋의 파일 목록(`git ls-tree`), PR의 커밋, PR이 닫겠다고 한 이슈(`gh api graphql`)를 담는다.
읽고 부르는 것은 app/worker/git/repo.py와 app/worker/git/github.py가 한다.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

REGULAR_FILE_MODES: Final = frozenset({"100644", "100755"})
MAX_FETCHED_ISSUES: Final = 10  # GitHub에서 받는 연결된 이슈 수 (다른 저장소의 이슈 포함)
MAX_FETCHED_COMMENTS: Final = 50  # 이슈마다 받는 댓글 수 (최근 것부터)


# --- git ---


@dataclass(frozen=True)
class TreeEntry:
    """base 커밋의 파일 하나 (`git ls-tree -l`). size는 blob이 아니면 None."""

    path: str
    mode: str
    sha: str
    size: int | None


@dataclass(frozen=True)
class RawCommit:
    """git에서 읽은 커밋 하나. 메시지는 이미 상한까지 읽었다(cut이면 더 있었다)."""

    sha: str
    parents: tuple[str, ...]
    message: str
    cut: bool = False


# --- GitHub 응답 ---


@dataclass(frozen=True)
class IssueNote:
    """이슈의 댓글 하나. association은 작성자와 저장소의 관계다."""

    association: str
    body: str


@dataclass(frozen=True)
class Issue:
    """연결된 이슈 하나 (GitHub의 응답 그대로). repo는 이슈가 있는 저장소("owner/name")다.
    comments는 받은 댓글(오래된 것부터), comments_total은 이슈의 전체 댓글 수다."""

    number: int
    repo: str
    title: str
    body: str
    comments: tuple[IssueNote, ...] = ()
    comments_total: int = 0


@dataclass(frozen=True)
class Linked:
    """PR이 닫겠다고 한 이슈 (`closingIssuesReferences`). total은 GitHub이 센 전체 수, fetched는
    받은 노드 수, unreadable은 받았지만 읽을 수 없던 노드(권한이 없거나 지워짐) 수다."""

    issues: tuple[Issue, ...] = ()
    total: int = 0
    fetched: int = 0
    unreadable: int = 0


def parse_closing_issues(data: Mapping[str, object]) -> Linked:
    """`gh api graphql`의 응답(`app/worker/git/github.py`의 CLOSING_ISSUES_QUERY)을 읽는다. 응답에
    `errors`가 있어도 연결된 이슈 목록을 읽을 수 있으면 읽는다(읽지 못한 노드는 null이라
    unreadable로 센다). 목록을 읽을 수 없으면 ValueError. 이슈가 아닌 노드(PR)는 뺀다."""
    try:
        refs = data["data"]["repository"]["pullRequest"]["closingIssuesReferences"]  # type: ignore[index]
        nodes = refs["nodes"]
        total = int(refs.get("totalCount") or 0)
    except (KeyError, TypeError, ValueError) as e:
        errors = data.get("errors") if isinstance(data, Mapping) else None
        raise ValueError(f"연결된 이슈 목록이 없음: {errors or e}") from e
    if not isinstance(nodes, list):
        raise ValueError("연결된 이슈 목록이 목록이 아님")
    issues: list[Issue] = []
    unreadable = 0
    for node in nodes:
        if not isinstance(node, Mapping):
            unreadable += 1
            continue
        if node.get("__typename", "Issue") != "Issue":
            continue
        try:
            comments = node.get("comments") or {}
            notes = tuple(
                IssueNote(str(c.get("authorAssociation") or ""), str(c.get("body") or ""))
                for c in comments.get("nodes") or []
                if isinstance(c, Mapping)
            )
            issues.append(Issue(
                number=int(node["number"]),
                repo=str((node.get("repository") or {}).get("nameWithOwner") or ""),
                title=str(node.get("title") or ""), body=str(node.get("body") or ""),
                comments=notes, comments_total=int(comments.get("totalCount") or len(notes)),
            ))  # fmt: skip
        except (KeyError, TypeError, ValueError, AttributeError):
            unreadable += 1
    return Linked(tuple(issues), max(total, len(nodes)), len(nodes), unreadable)
