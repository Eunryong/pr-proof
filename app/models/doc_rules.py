"""문서 규칙 경로의 결과 (docs/DESIGN.md 5.1 load_rules → author_checker → detect, 13절 1단계).

그래프 상태에 들어가므로 데이터만 둔다. 검사기 본문은 상태에 넣지 않고 `checkers` 테이블의 키만
넣는다 (5.5). 결과 코멘트의 "문서 규칙" 절은 `DocRules` 값 하나로만 그린다.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from app.models.checker import CheckerKind
from app.models.checker_authoring import UncheckedKind
from app.models.db import ModelCall
from app.models.rule import Rule, UnreadDoc
from app.models.violation import Violation

# 규칙 읽기와 검사기 작성 (규칙 묶음 상한에서 세는 단계)
DOC_RULE_STAGES: Final = frozenset({"rules", "checker"})


@dataclass(frozen=True)
class CheckerRef:
    """채택한 검사기. 본문과 예시는 `checkers` 테이블에서 key로 찾는다."""

    key: str  # checker_authoring.checker_key
    id: str
    kind: CheckerKind
    cached: bool  # 저장된 검사기를 썼다 (모델 호출 0)


@dataclass(frozen=True)
class RuleChecked:
    rule: Rule
    checker: CheckerRef


@dataclass(frozen=True)
class RuleUnchecked:
    rule: Rule
    kind: UncheckedKind
    detail: str


RuleOutcome = RuleChecked | RuleUnchecked


@dataclass(frozen=True)
class RulesRead:
    """규칙 읽기의 결과. 검사기 작성과 위반 찾기로 넘어간다."""

    key: str  # 규칙 묶음의 캐시 키
    rules: tuple[Rule, ...]  # `rules.disabled`를 뺀 것
    disabled: int  # `rules.disabled`로 뺀 규칙 수


@dataclass(frozen=True)
class CallUsage:
    """이번 검사에서 규칙 읽기와 검사기 작성이 쓴 모델 호출 (9.4)."""

    calls: int  # 상한에서 센 값의 합
    limit: int  # 규칙 묶음 상한
    cache_hits: int
    models: tuple[str, ...]
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class NotRun:
    """이 경로를 끝까지 돌지 못했다. v1 결과와 테스트는 그대로 나온다."""

    reason: str


@dataclass(frozen=True)
class NoRuleDocs:
    unread: tuple[UnreadDoc, ...] = ()  # 있었지만 읽지 못한 문서


@dataclass(frozen=True)
class NoRulesFound:
    dropped: int = 0  # 모델이 답했지만 원문 확인에서 버린 규칙 수


@dataclass(frozen=True)
class Found:
    outcomes: tuple[RuleOutcome, ...]  # 적용한 규칙마다 하나, 읽은 순서
    violations: tuple[Violation, ...]
    disabled: int
    usage: CallUsage


DocRules = NotRun | NoRuleDocs | NoRulesFound | Found

# 체크포인트에서 되살릴 수 있게 graph.py가 등록하는 타입
STATE_TYPES: Final = (
    ("app.models.doc_rules", "CheckerRef"),
    ("app.models.doc_rules", "RuleChecked"),
    ("app.models.doc_rules", "RuleUnchecked"),
    ("app.models.doc_rules", "RulesRead"),
    ("app.models.doc_rules", "CallUsage"),
    ("app.models.doc_rules", "NotRun"),
    ("app.models.doc_rules", "NoRuleDocs"),
    ("app.models.doc_rules", "NoRulesFound"),
    ("app.models.doc_rules", "Found"),
    ("app.models.rule", "Rule"),
    ("app.models.rule", "RuleSource"),
    ("app.models.rule", "CheckMethod"),
    ("app.models.rule", "Example"),
    ("app.models.rule", "UnreadDoc"),
    ("app.models.violation", "Violation"),
)


def with_run_failures(
    outcomes: Sequence[RuleOutcome], failed: Iterable[RuleUnchecked]
) -> tuple[RuleOutcome, ...]:
    """검사기가 있었지만 이 PR의 파일에서 돌지 못한 규칙을 검사하지 못한 규칙으로 바꾼다."""
    by_id = {u.rule.id: u for u in failed}
    return tuple(by_id.get(o.rule.id, o) for o in outcomes)


def call_usage(calls: Iterable[ModelCall], limit: int) -> CallUsage:
    """calls는 이번 검사의 규칙 읽기·검사기 작성 호출 기록이다."""
    rows = [c for c in calls if c.stage in DOC_RULE_STAGES]
    made = [c for c in rows if not c.cache_hit]
    return CallUsage(
        calls=sum(c.counted for c in made),
        limit=limit,
        cache_hits=sum(1 for c in rows if c.cache_hit),
        models=tuple(sorted({c.model_id or c.model for c in made})),
        input_tokens=sum(c.input_tokens or 0 for c in made),
        output_tokens=sum(c.output_tokens or 0 for c in made),
    )
