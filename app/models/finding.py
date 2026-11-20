"""v1 lint 경로가 내는 위반과 그 결과 (docs/DESIGN.md F6). 3단계에 도구 규칙으로 옮긴다."""

from dataclasses import dataclass
from typing import Literal

Skill = Literal["lint"]
Severity = Literal["error", "warning"]


@dataclass(frozen=True)
class Finding:
    file: str  # 리포 기준 경로
    line: int
    severity: Severity
    message: str
    skill: Skill
    source: str  # 규칙의 출처. 예: "pyproject.toml", "기본 규칙"
    code: str = ""  # 도구의 규칙 코드. 예: "F401"
    fixable: bool = False  # 도구가 동작을 바꾸지 않는 수정을 낼 수 있다 (lint: ruff의 safe fix)
    # 쓰지 않는다. 2단계 전 체크포인트의 Finding을 되살리려고 남겨 둔다 (필드가 빠지면 dict가 된다)
    covered: bool | None = None
    # 위반 줄의 내용 (앞뒤 공백 제거). 거절 기록과 맞춰 볼 때 쓴다 (app/models/rejection.py)
    line_text: str | None = None
    # 같은 줄을 고친 제안 PR을 사람이 머지하지 않고 닫았다 (그 번호). 자동 수정 대상에서 뺀다
    rejected_in: int | None = None


@dataclass(frozen=True)
class SkillResult:
    """스킬 하나의 검사 결과. error가 있으면 검사하지 못한 것이고 findings는 비어 있다."""

    skill: Skill
    findings: tuple[Finding, ...] = ()
    source: str = ""
    error: str | None = None

    @property
    def checked(self) -> bool:
        return self.error is None

    @property
    def fixable_count(self) -> int:
        """자동으로 고칠 수 있는 위반 수. 이전에 거절된 위반은 세지 않는다."""
        return sum(1 for f in self.findings if f.fixable and f.rejected_in is None)
