"""제안 PR 규칙 (docs/DESIGN.md F10): 브랜치 이름, 푸시 조건(상한·비밀값), 본문. 판단만 한다."""

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from app.models.finding import Finding

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
MIN_LITERAL_SECRET: Final = 8  # 너무 짧은 값은 흔한 문자열과 겹친다
# 위반 범위(블록 전체)를 다시 쓰는 규칙. PR이 블록의 한 줄만 바꿔도 기존 줄 순서가 바뀔 수 있다
BLOCK_REWRITE_CODES: Final = frozenset({"I001"})


def branch_name(pr_number: int, skill: str) -> str:
    return f"{BRANCH_PREFIX}pr-{pr_number}-fix-{skill}"


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
    added = "\n".join(_added_lines(patch))
    for literal in literals:
        if len(literal) >= MIN_LITERAL_SECRET and literal in added:
            return "파이프라인 비밀값"
    for pattern in SECRET_PATTERNS:
        if pattern.search(added):
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
        return f"비밀값으로 보이는 내용이 있음 ({kind})"
    return None


def fixed_codes(before: Sequence[Finding], remaining: Sequence[Finding]) -> Counter:
    """고친 위반을 규칙 코드별로 센다."""
    left = Counter(f.code for f in remaining)
    return Counter(f.code for f in before) - left


def title(pr_number: int, skill: str, fixed: int) -> str:
    what = f"위반 {fixed}건 수정" if fixed else "포맷 정리"
    return f"[pipeline] #{pr_number} {skill} {what}"


def body(
    pr_number: int,
    skill: str,
    head_sha: str,
    codes: Counter,
    source: str,
    tests: str,
    formatted: int,
) -> str:
    lines = [
        PROPOSAL_MARKER,
        f"#{pr_number}의 커밋 `{head_sha[:7]}`에서 PR이 바꾼 줄의 {skill} 위반을 고친 "
        "수정안입니다.",
        "",
    ]
    if codes:
        lines.append(
            "고친 위반: " + ", ".join(f"`{code}` {n}건" for code, n in sorted(codes.items()))
        )
    lines.append(f"적용한 규칙: ruff ({source}). ruff의 안전한 수정만 썼습니다.")
    rewrote = sorted(BLOCK_REWRITE_CODES & set(codes))
    if rewrote:
        lines.append(
            f"{', '.join(f'`{c}`' for c in rewrote)}(import 정렬)는 import 블록 전체를 다시 "
            "정렬했습니다. 블록 안의 기존 줄 순서가 바뀌었을 수 있습니다."
        )
    if formatted:
        lines.append(
            f"`ruff format`은 PR이 바꾼 파일 {formatted}개의 전체에 적용했습니다. "
            "base 브랜치가 이미 포맷돼 있어 기존 줄은 대부분 그대로입니다."
        )
    if not rewrote and not formatted:
        lines.append("PR이 건드리지 않은 줄은 바꾸지 않았습니다.")
    lines += [
        f"고친 뒤 테스트: {tests} (수정 전과 같은 결과)",
        "",
        f"머지하면 #{pr_number}의 브랜치에 들어갑니다. 원하지 않으면 닫아 주세요. "
        "같은 커밋에는 다시 제안하지 않습니다.",
    ]
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class ProposalResult:
    """제안 PR을 열었으면 number, 못 열었으면 reason."""

    number: int | None = None
    url: str = ""
    reason: str = ""
    updated: bool = False  # 같은 브랜치의 열린 제안을 새 커밋으로 바꿈
