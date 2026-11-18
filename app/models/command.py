"""PR 코멘트 명령 해석 (docs/DESIGN.md F11). 판단만 하고 실행하지 않는다."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

CommandName = Literal["recheck", "fix", "extension", "unknown"]

RECHECK: Final = "/recheck"
FIX: Final = "/fix"
# 확장 명령 → .pipeline.yaml의 extensions.<이름> (7단계)
EXTENSION_COMMANDS: Final[Mapping[str, str]] = {
    "/mutate": "mutation",
    "/generate-tests": "ai_tests",
}


@dataclass(frozen=True)
class ParsedCommand:
    name: CommandName
    word: str  # 코멘트에 쓴 명령어. 예: "/mutate"
    arg: str = ""  # "/fix lint"의 "lint", 확장 명령이면 extensions의 이름


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
    if word in EXTENSION_COMMANDS:
        return ParsedCommand("extension", word, EXTENSION_COMMANDS[word])
    return ParsedCommand("unknown", word)
