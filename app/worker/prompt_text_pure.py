"""모델에게 주는 지시문(시스템 프롬프트)의 글. 글은 `app/worker/prompts/` 아래의 마크다운 파일에
있고, 단계의 모듈은 import할 때 여기서 읽어 상수로 둔다(목록은 그 폴더의 README.md).

읽는 곳은 이 패키지의 그 폴더뿐이다. 이름은 코드에 적은 것만 받고, 대상 저장소나 PR에서 온 값으로
경로를 만들지 않는다. 파일이 없으면 import에서 실패하므로 워커는 작업 도중이 아니라 시작할 때
멈춘다. 프롬프트 판은 읽은 글의 해시라서 파일을 한 글자라도 고치면 달라진다.
"""

import re
from pathlib import Path
from string import Template
from typing import Final

PROMPT_DIR: Final = Path(__file__).parent / "prompts"
_NAME: Final = re.compile(r"[a-z_]+(\.[a-z_]+)?")


def load(name: str, **values: object) -> str:
    """`prompts/<name>.md`의 글. 파일 끝의 줄바꿈 하나는 뺀다.

    values를 주면 글의 `$이름` 자리에 그 값을 넣는다. 자리가 남거나 값이 남으면 오류다.
    """
    if not _NAME.fullmatch(name):
        raise ValueError(f"프롬프트 이름이 아니다: {name!r}")
    text = (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8").removesuffix("\n")
    if not values:
        return text
    template = Template(text)
    unused = set(values) - set(template.get_identifiers())
    if unused:
        raise ValueError(f"프롬프트 {name}에 없는 자리: {', '.join(sorted(unused))}")
    return template.substitute(values)
