"""모델에게 주는 지시문(시스템 프롬프트)의 글. 글은 그것을 쓰는 단계의 폴더 아래 `prompts/`의
마크다운 파일에 있고(`rules/prompts/`, `fix/prompts/`, `feedback/prompts/`), 단계의 모듈은
import할 때 여기서 읽어 상수로 둔다. 폴더마다 README.md에 목록이 있다.

읽는 곳은 부르는 모듈 옆의 `prompts/` 폴더뿐이다(`load(__file__, "이름")`). 이름은 코드에 적은 것만
받고, 대상 저장소나 PR에서 온 값으로 경로를 만들지 않는다. 파일이 없으면 import에서 실패하므로
워커는 작업 도중이 아니라 시작할 때 멈춘다. 프롬프트 판은 읽은 글의 해시라서 파일을 한 글자라도
고치면 달라진다.
"""

import re
from pathlib import Path
from string import Template
from typing import Final

PROMPT_FOLDER: Final = "prompts"
_NAME: Final = re.compile(r"[a-z_]+(\.[a-z_]+)?")
_SLOT: Final = re.compile(r"\{\{([a-z_]+)\}\}")


def load(module_file: str, name: str, **values: object) -> str:
    """부르는 모듈(`module_file`은 그 모듈의 `__file__`) 옆 `prompts/<name>.md`의 글. 파일 끝의
    줄바꿈 하나는 뺀다.

    values를 주면 글의 `$이름` 자리에 그 값을 넣는다. 자리가 남거나 값이 남으면 오류다.
    """
    if not _NAME.fullmatch(name):
        raise ValueError(f"프롬프트 이름이 아니다: {name!r}")
    path = Path(module_file).parent / PROMPT_FOLDER / f"{name}.md"
    text = path.read_text(encoding="utf-8").removesuffix("\n")
    if not values:
        return text
    template = Template(text)
    unused = set(values) - set(template.get_identifiers())
    if unused:
        raise ValueError(f"프롬프트 {name}에 없는 자리: {', '.join(sorted(unused))}")
    return template.substitute(values)


def fill(text: str, **pieces: str) -> str:
    """글의 `{{이름}}` 자리에 조각을 끼운다. 자리는 한 줄을 혼자 차지한다. 조각이 빈 글이면 그
    줄을 없앤다. 줄 가운데에 있는 자리, 조각이 없는 자리, 자리가 없는 조각은 오류다.

    `load`의 `$이름`과 따로다. 조각의 글에 `$`가 있어도(ast-grep의 `$A`) 그대로 둔다.
    """
    lines: list[str] = []
    used: set[str] = set()
    for line in text.split("\n"):
        slot = _SLOT.fullmatch(line.strip())
        if slot is None:
            if _SLOT.search(line):
                raise ValueError(f"자리는 한 줄을 혼자 차지해야 한다: {line.strip()!r}")
            lines.append(line)
            continue
        name = slot.group(1)
        if name not in pieces:
            raise ValueError(f"조각이 없는 자리: {name}")
        used.add(name)
        if pieces[name]:
            lines.append(pieces[name])
    unused = set(pieces) - used
    if unused:
        raise ValueError(f"글에 없는 자리: {', '.join(sorted(unused))}")
    return "\n".join(lines)
