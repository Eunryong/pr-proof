"""PR 코멘트에 믿지 않는 글(모델의 글, 문서의 인용, 파일 경로, 코드)을 넣는 방법.

믿지 않는 글은 코드 울타리나 코드 스팬 안에만 넣는다. 그 안에서는 마크다운, HTML, `@` 멘션이
동작하지 않는다. 울타리는 글 안의 어떤 백틱 줄보다 길게 만들어 글이 울타리를 닫지 못하게 한다.
"""

import re
from typing import Final

_BACKTICKS: Final = re.compile(r"`+")
_SPACE: Final = re.compile(r"\s+")


def code_fence(text: str) -> str:
    """text 안의 어떤 백틱 줄보다 긴 코드 울타리. 본문의 ```가 블록을 일찍 닫지 않게 한다."""
    longest = max((len(m) for m in _BACKTICKS.findall(text)), default=0)
    return "`" * max(3, longest + 1)


def inline_code(text: str, limit: int = 300) -> str:
    """한 줄 코드 스팬. 줄바꿈은 공백으로 모으고, limit자를 넘으면 자른다."""
    text = _SPACE.sub(" ", text).strip()
    if len(text) > limit:
        text = text[:limit] + "…"
    if not text:
        return "` `"
    fence = "`" * (max((len(m) for m in _BACKTICKS.findall(text)), default=0) + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"
