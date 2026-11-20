"""PR 코멘트 명령 해석 (docs/DESIGN.md 4.9 명령). 판단만 하고 실행하지 않는다."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

CommandName = Literal["recheck", "fix", "unknown"]

RECHECK: Final = "/recheck"
FIX: Final = "/fix"
# `/fix <인자>` → 다시 돌릴 수정 묶음. 비면 수정할 것이 남은 묶음 전부다 (4.9).
# `mechanical`은 v1 lint 경로다. 3단계에서 lint를 새 구조로 옮길 때까지 `lint`와 같다.
# `static`은 v1 static을 지울 때 함께 지운다
FIX_TARGETS: Final[Mapping[str, tuple[str, ...]]] = {
    "": ("lint", "static", "model"),
    "mechanical": ("lint",),
    "model": ("model",),
    "lint": ("lint",),
    "static": ("static",),
}


@dataclass(frozen=True)
class ParsedCommand:
    name: CommandName
    word: str  # 코멘트에 쓴 명령어. 예: "/fix"
    arg: str = ""  # "/fix model"의 "model"


def parse_command(text: str) -> ParsedCommand:
    """코멘트 첫 줄을 해석한다. 모르는 명령은 unknown.

    모르는 명령에는 답하지 않는다. `/cc` 같은 다른 관례일 수 있다.
    """
    word, _, rest = text.strip().partition(" ")
    word = word.lower()
    if word == RECHECK:
        return ParsedCommand("recheck", word)
    if word == FIX:
        return ParsedCommand("fix", word, rest.strip().lower())
    return ParsedCommand("unknown", word)
