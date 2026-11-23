"""수정안 PR 규칙 (docs/DESIGN.md 4.8, 4.9): 브랜치 이름, 푸시 조건(상한·비밀값). 판단만 한다."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from app.worker.core.cache_key_pure import text_hash
from app.worker.core.secret_pure import find_secret

BRANCH_PREFIX: Final = "pipeline/"
MAX_FILES: Final = 20
MAX_CHANGED_LINES: Final = 500
PROPOSAL_MARKER: Final = "<!-- pr-proof:proposal -->"

SECRET_BLOCKED: Final = "비밀값으로 보이는 내용이 있음"
# 위반 범위(블록 전체)를 다시 쓰는 규칙. PR이 블록의 한 줄만 바꿔도 기존 줄 순서가 바뀔 수 있다
BLOCK_REWRITE_CODES: Final = frozenset({"I001"})


def branch_name(pr_number: int, skill: str) -> str:
    return f"{BRANCH_PREFIX}pr-{pr_number}-fix-{skill}"


def review_marker(patch: str) -> str:
    """수정안 PR의 줄 리뷰에 넣는 표시 (5.6). 같은 패치의 리뷰가 이미 있으면 다시 달지 않는다."""
    return f"<!-- pr-proof:review {text_hash(patch)[:16]} -->"


def changed_line_count(patch: str) -> int:
    return sum(
        1
        for line in patch.splitlines()
        if (line.startswith("+") and not line.startswith("+++"))
        or (line.startswith("-") and not line.startswith("---"))
    )


def push_blocker(patch: str, files: Sequence[str], literals: Iterable[str] = ()) -> str | None:
    """제안 PR을 올리면 안 되는 이유. 올려도 되면 None."""
    if not files or not patch.strip():
        return "바뀐 내용이 없음"
    if len(files) > MAX_FILES:
        return f"바뀐 파일이 {len(files)}개로 상한({MAX_FILES}개)을 넘음"
    lines = changed_line_count(patch)
    if lines > MAX_CHANGED_LINES:
        return f"바뀐 줄이 {lines}줄로 상한({MAX_CHANGED_LINES}줄)을 넘음"
    kind = find_secret(patch, literals)
    if kind is not None:
        return f"{SECRET_BLOCKED} ({kind})"
    return None


@dataclass(frozen=True)
class ProposalResult:
    """제안 PR을 열었으면 number, 못 열었으면 reason."""

    number: int | None = None
    url: str = ""
    reason: str = ""
    updated: bool = False  # 같은 브랜치의 열린 제안을 새 커밋으로 바꿈

    @property
    def secret_blocked(self) -> bool:
        """비밀값 때문에 막았다. 수정 내용도 코멘트에 싣지 않는다."""
        return self.reason.startswith(SECRET_BLOCKED)
