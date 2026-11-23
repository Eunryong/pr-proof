"""규칙 경로의 결과 (docs/DESIGN.md 5.1 load_rules → author_checker → detect).

규칙은 문서에서 읽은 것과 도구 설정에서 온 것(ruff 설정)이다. 그래프 상태에 들어가므로 데이터만
둔다. 검사기 본문은 상태에 넣지 않고 `checkers` 테이블의 키만 넣는다 (5.5). 결과 코멘트의 규칙 절은
`DocRules` 값 하나로만 그린다.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from app.shared.db_pure import ModelCall
from app.worker.checker_authoring_pure import JudgeRoute, UncheckedKind
from app.worker.core.checker_pure import CheckerKind
from app.worker.core.rule_pure import Rule, UnreadDoc
from app.worker.core.violation_pure import Violation
from app.worker.rules.judge_pure import JudgeUsage
from app.worker.store.rag_pure import Retrieval

# 규칙 읽기와 검사기 작성 (규칙 묶음 상한에서 세는 단계)
DOC_RULE_STAGES: Final = frozenset({"rules", "checker"})


@dataclass(frozen=True)
class CheckerRef:
    """채택한 검사기. 본문과 예시는 `checkers` 테이블에서 key로 찾는다."""

    key: str  # `checkers` 테이블의 키 (검사기 작성, 도구 선택, 도구 설정)
    id: str
    kind: CheckerKind
    cached: bool  # 저장된 검사기를 썼다 (모델 호출 0)
    codes: tuple[str, ...] = ()  # 도구(ruff)로 확인하는 문서 규칙의 ruff 코드. 코멘트에 보인다


@dataclass(frozen=True)
class RuleChecked:
    rule: Rule
    checker: CheckerRef


@dataclass(frozen=True)
class RuleUnchecked:
    rule: Rule
    kind: UncheckedKind
    detail: str


# 모델 판단을 다 하지 못한 이유: 호출 상한, 모델 호출 실패 (4.5 "실패 시")
JudgeGap = Literal["cap", "model_failed"]


@dataclass(frozen=True)
class RuleJudged:
    """검사기를 채택하지 않아 모델이 바뀐 hunk를 직접 보는 규칙 (4.4의 3번, 4.5 모델의 직접 판단).

    route는 모델 판단으로 보낸 까닭이다(`JudgeRoute`). left는 이번 검사에서 판단하지 못한 (이
    규칙, hunk) 쌍의 수다(호출 상한, 모델 호출 실패). 0이면 다 물었다. pairs는 본 쌍의 수,
    undecided는 그중 모델이 이 자료로는 판단할 수 없다고 답한 쌍의 수와 그 까닭이다.
    """

    rule: Rule
    # none·part면 검사기 작성 모델이 적은 "검사기로 가릴 수 없는 부분", 아니면 검사기 작성의
    # 마지막 실패
    reason: str
    # 기본값은 이 필드를 더하기 전에 저장된 그래프 상태(체크포인트)를 읽기 위한 것이다. 그때의 뜻은
    # "회차를 다 씀"이었다
    route: JudgeRoute = "attempts"
    left: int = 0
    left_kind: JudgeGap | None = None
    left_detail: str = ""
    pairs: int = 0
    undecided: int = 0
    undecided_why: str = ""


RuleOutcome = RuleChecked | RuleUnchecked | RuleJudged


@dataclass(frozen=True)
class RunJudged:
    """run 규칙 하나에 모델이 테스트 뒤에 낸 답 (4.5 "실행 결과의 규칙"). kept는 지킴, broken은
    어김, undecided는 모델이 이 자료로는 판단할 수 없다고 답함, left는 묻지 못함(호출 상한, 모델
    호출 실패). facts는 모델에게 준 실행 결과의 요약, why는 모델의 까닭이나 묻지 못한 이유다."""

    rule_id: str
    verdict: Literal["kept", "broken", "undecided", "left"]
    why: str
    facts: str


@dataclass(frozen=True)
class RulesRead:
    """규칙 읽기의 결과. 검사 방법 정하기와 위반 찾기로 넘어간다."""

    key: str  # 문서 규칙 묶음의 캐시 키. 문서를 읽지 못했으면 ""
    rules: tuple[Rule, ...]  # `rules.disabled`를 뺀 것. 문서 규칙 다음에 도구 설정의 규칙
    disabled: int  # `rules.disabled`로 뺀 규칙 수
    # 도구 설정의 규칙만으로 검사할 때 규칙 문서를 읽지 못한 이유 (4.3 "실패 시")
    note: str = ""
    sections: tuple[str, ...] = ()  # 규칙 문서가 커서 관련 절만 읽었으면 그 절 (6.3)


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
    note: str = ""  # RulesRead.note
    judge: JudgeUsage | None = None  # 모델 판단을 한 검사만
    retrievals: tuple[Retrieval, ...] = ()  # 검사기 작성이 넣은 과거 기록 (검색을 켠 저장소)
    sections: tuple[str, ...] = ()  # RulesRead.sections
    runs: tuple[RunJudged, ...] = ()  # run 규칙의 답. 테스트 뒤에 채운다


DocRules = NotRun | NoRuleDocs | NoRulesFound | Found

# 체크포인트에서 되살릴 수 있게 graph.py가 등록하는 타입
STATE_TYPES: Final = (
    ("app.worker.doc_rules_pure", "CheckerRef"),
    ("app.worker.doc_rules_pure", "RuleChecked"),
    ("app.worker.doc_rules_pure", "RuleUnchecked"),
    ("app.worker.doc_rules_pure", "RuleJudged"),
    ("app.worker.doc_rules_pure", "RunJudged"),
    ("app.worker.rules.judge_pure", "JudgeUsage"),
    ("app.worker.doc_rules_pure", "RulesRead"),
    ("app.worker.doc_rules_pure", "CallUsage"),
    ("app.worker.doc_rules_pure", "NotRun"),
    ("app.worker.doc_rules_pure", "NoRuleDocs"),
    ("app.worker.doc_rules_pure", "NoRulesFound"),
    ("app.worker.doc_rules_pure", "Found"),
    ("app.worker.core.rule_pure", "Rule"),
    ("app.worker.core.rule_pure", "RuleSource"),
    ("app.worker.core.rule_pure", "CheckMethod"),
    ("app.worker.core.rule_pure", "Example"),
    ("app.worker.core.rule_pure", "UnreadDoc"),
    ("app.worker.core.violation_pure", "Violation"),
)


def outcomes_by_rule(found: Found) -> dict[str, RuleOutcome]:
    """위반의 규칙 id → 그 규칙의 결과. 도구 설정 규칙의 위반은 ruff 코드가 규칙 id라(4.3) 그 코드도
    도구 설정 규칙을 가리킨다."""
    by_id = {o.rule.id: o for o in found.outcomes}
    config = next((o for o in found.outcomes if o.rule.origin == "tool_config"), None)
    if config is not None:
        for v in found.violations:
            if v.rule_id not in by_id and v.rule_id == v.code:
                by_id[v.rule_id] = config
    return by_id


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
