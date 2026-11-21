"""규칙 위반 (docs/DESIGN.md 7.3). 데이터와 id 계산만 한다."""

import hashlib
import json
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Violation:
    id: str  # violation_id. 줄 번호를 쓰지 않는다
    rule_id: str
    file: str
    line: int  # 파일 단위 위반이면 0
    message: str  # 도구의 메시지 또는 모델의 why
    found_by: Literal["tool", "checker", "model"]
    code: str = ""  # 도구의 규칙 코드
    line_text: str | None = None  # 앞뒤 공백을 뺀 줄 내용. 거절 기록과 맞춰 본다
    mechanical_fix: bool = False  # 도구가 동작을 바꾸지 않는 수정을 낼 수 있다
    covered: bool | None = None  # 테스트가 그 줄을 지나가는지. None은 모름
    rejected_in: int | None = None  # 같은 줄을 고친 수정안을 사람이 닫았으면 그 PR 번호
    # file: 줄이 아니라 파일 자체(이름·경로)가 규칙을 어겼다 (경로 패턴 검사기)
    unit: Literal["line", "file"] = "line"


def violation_id(rule_id: str, file: str, line_text: str, occurrence: int) -> str:
    """(규칙 id, 파일, 줄 내용, 그 파일에서 같은 줄 내용의 몇 번째인지)의 해시.

    위쪽에 줄이 더해져 줄 번호가 바뀌어도 같은 위반은 같은 id다.
    """
    payload = json.dumps([rule_id, file, line_text.strip(), occurrence], ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]
