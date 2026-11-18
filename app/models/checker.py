"""모델이 쓴 검사기 (docs/DESIGN.md 7.2). 데이터와 id 계산만 한다."""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from app.models.rule import Example


@dataclass(frozen=True)
class Checker:
    id: str  # 본문 해시 (checker_id)
    rule_id: str
    rule_text_hash: str
    kind: Literal["ast-grep", "python-ast"]
    body: str  # 규칙 YAML 또는 스크립트
    examples: tuple[Example, ...]
    engine_version: str  # 검사 엔진 버전. 바뀌면 예시 확인을 다시 한다
    model: str = ""
    verified_at: datetime | None = None


def checker_id(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()
