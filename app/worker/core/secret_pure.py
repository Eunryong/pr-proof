"""비밀값으로 보이는 것 찾기 (docs/DESIGN.md 4.8, 4.10). 수정안 PR의 푸시 조건과 기록 저장 전 검사가
같이 쓴다. 값은 돌려주지 않고 종류만 돌려준다. 계산만 한다."""

import re
from collections.abc import Iterable
from typing import Final

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


def _added_lines(patch: str) -> list[str]:
    return [
        line[1:]
        for line in patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]


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
