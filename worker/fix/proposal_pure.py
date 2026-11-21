"""수정안 PR 규칙 (docs/DESIGN.md 4.8, 4.9): 브랜치 이름, 푸시 조건(상한·비밀값). 판단만 한다."""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from worker.cache_key_pure import text_hash

BRANCH_PREFIX: Final = "pipeline/"
MAX_FILES: Final = 20
MAX_CHANGED_LINES: Final = 500
PROPOSAL_MARKER: Final = "<!-- pr-proof:proposal -->"

# 토큰·키로 보이는 패턴. 파이프라인이 가진 비밀값(웹훅 시크릿)은 따로 그대로 찾는다
SECRET_PATTERNS: Final = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{40,}"),
    re.compile(r"xox[abpors]-[A-Za-z0-9-]{10,}"),
    re.compile(r"hooks\.slack\.com/services/[A-Za-z0-9/]+"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
)
SECRET_BLOCKED: Final = "비밀값으로 보이는 내용이 있음"
MIN_LITERAL_SECRET: Final = 8  # 너무 짧은 값은 흔한 문자열과 겹친다
# 위반 범위(블록 전체)를 다시 쓰는 규칙. PR이 블록의 한 줄만 바꿔도 기존 줄 순서가 바뀔 수 있다
BLOCK_REWRITE_CODES: Final = frozenset({"I001"})


def branch_name(pr_number: int, skill: str) -> str:
    return f"{BRANCH_PREFIX}pr-{pr_number}-fix-{skill}"


def review_marker(patch: str) -> str:
    """수정안 PR의 줄 리뷰에 넣는 표시 (5.6). 같은 패치의 리뷰가 이미 있으면 다시 달지 않는다."""
    return f"<!-- pr-proof:review {text_hash(patch)[:16]} -->"


def _added_lines(patch: str) -> list[str]:
    return [
        line[1:]
        for line in patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]


def changed_line_count(patch: str) -> int:
    return sum(
        1
        for line in patch.splitlines()
        if (line.startswith("+") and not line.startswith("+++"))
        or (line.startswith("-") and not line.startswith("---"))
    )


def find_secret(patch: str, literals: Iterable[str] = ()) -> str | None:
    """추가된 줄에 비밀값으로 보이는 것이 있으면 그 종류.

    값 자체는 돌려주지 않는다 (로그와 코멘트에 남지 않게).
    """
    return find_secret_in_text("\n".join(_added_lines(patch)), literals)


def find_secret_in_text(text: str, literals: Iterable[str] = ()) -> str | None:
    """글(모델이 쓴 설명 등) 전체에서 비밀값으로 보이는 것의 종류. 값은 돌려주지 않는다."""
    for literal in literals:
        if len(literal) >= MIN_LITERAL_SECRET and literal in text:
            return "파이프라인 비밀값"
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            return "토큰·키 형식"
    return None


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
