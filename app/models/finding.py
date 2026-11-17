"""스킬(lint·static·structure)이 내는 위반과 스킬 하나의 결과 (docs/DESIGN.md F6)."""

from dataclasses import dataclass
from typing import Literal

Skill = Literal["lint", "static", "structure"]
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
        return sum(1 for f in self.findings if f.fixable)
