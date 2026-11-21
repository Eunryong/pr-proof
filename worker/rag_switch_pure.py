"""과거 기록 검색을 켜는 조건 (docs/DESIGN.md 6.7 "검색을 켜는 조건", 10절 `rag.enabled`).

`rag.enabled`는 `auto`(기본) / `on` / `off`다. `on`과 `off`는 조건을 보지 않는다. `auto`는 SQLite에
있는 숫자만 보고 정한다:

1. 이 저장소에서 `auto`가 검색을 다시 끈 기록(`rag_auto_off`)이 지금 기준값으로 남아 있으면 끔.
2. 이 저장소의 채택·거절 기록(`records`의 kind가 adopted·rejected, 출처는 수정안 PR과 작성자 커밋
   둘 다)이 `min_records`건 미만이면 끔. 그 전에는 임베딩 모델도 Chroma도 열지 않는다.
3. 모델 수정 묶음의 수정 시도 중 수정안 PR이 머지(채택)됐거나 사람이 닫은(거절) 것을, 검색으로
   넣은 기록이 있는 쪽(`references_json`이 빈 배열이 아님)과 없는 쪽으로 나눈다. 있는 쪽이
   `min_attempts`건 이상 쌓였을 때 있는 쪽의 채택률이 없는 쪽보다 낮으면(같으면 켜 둠) 끔 (저장소
   주인의 정의). 없는 쪽이 `min_baseline`건 미만이면 견줄 근거가 없어 끄지 않는다. 끌 때 1의 기록을
   남기므로 다시 켜지지 않는다.
4. 그 밖은 켬.

이 모듈은 외부 호출이 없다. 숫자를 읽고 1의 기록을 쓰는 것은 worker/memory.py의 `switch_for`다.
"""

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final, Literal

from worker.fix.model_fix_pure import MODEL_BUNDLE

RagMode = Literal["auto", "on", "off"]
MODES: Final = ("auto", "on", "off")
DEFAULT_MODE: Final = "auto"
# 초깃값이다. 측정한 뒤 조정한다 (6.7). 테스트는 상수를 바꾸지 않고 함수 인자로 넘긴다
AUTO_MIN_RECORDS: Final = 20  # auto가 검색을 켜는 이 저장소의 채택·거절 기록 수
# auto가 채택률을 견주기 시작하는, 검색으로 기록을 넣은 수정 시도 중 결과를 아는 수 (저장소 주인)
AUTO_MIN_ATTEMPTS: Final = 30
# 견줄 쪽(기록을 넣지 않은 수정 시도)의 최소 수. 이보다 적으면 끄지 않는다 (작업의 읽기, 6.7)
AUTO_MIN_BASELINE: Final = 10
COUNTED_KINDS: Final = frozenset({"adopted", "rejected"})
# 수정 시도의 결과를 아는 수정안 PR 상태. 파이프라인이 닫은 것(superseded, closed_with_pr)과 열린
# 것은 사람이 정하지 않아 모른다
OUTCOME_STATES: Final = frozenset({"merged", "rejected"})
_STATE_RANK: Final[Mapping[str, int]] = {"merged": 2, "rejected": 1}

Why = Literal["setting", "few_records", "enough_records", "worse"]


def parse_mode(value: object) -> RagMode | None:
    """`.pipeline.yaml`의 `rag.enabled` 값. 옛 불리언은 `true` = on, `false` = off다 (YAML 1.1은
    따옴표 없는 `on`·`off`도 불리언으로 읽는다). 그 밖의 값은 None (기본값으로 읽고 알린다)."""
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, str) and value in MODES:
        return value  # type: ignore[return-value]
    return None


@dataclass(frozen=True)
class Outcomes:
    """결과를 아는 수정 시도 한 무리. 채택률은 adopted / known이다."""

    adopted: int = 0
    known: int = 0


@dataclass(frozen=True)
class Evidence:
    """auto가 보는 숫자. 기록의 글은 들지 않는다."""

    records: int  # 이 저장소의 채택·거절 기록 수
    with_refs: Outcomes = Outcomes()  # 검색으로 넣은 기록이 있는 수정 시도
    without_refs: Outcomes = Outcomes()


@dataclass(frozen=True)
class FixOutcome:
    """수정안 PR 하나와 그것이 올린 수정 시도. SQLite의 `proposal`과 `fix_attempts`를 이은 줄."""

    attempt_id: int
    bundle: str
    state: str  # ProposalState
    references_json: str | None


def _has_references(references_json: str | None) -> bool:
    if not references_json:
        return False
    try:
        ids = json.loads(references_json)
    except ValueError:
        return False
    return isinstance(ids, list) and len(ids) > 0


def outcomes(rows: Iterable[FixOutcome]) -> tuple[Outcomes, Outcomes]:
    """(검색 기록이 있는 쪽, 없는 쪽). 모델 수정 묶음만 센다(기계적 수정에는 검색을 쓰지 않는다).
    수정안 PR이 머지됐으면 채택, 사람이 닫았으면 거절이고, 그 밖의 상태와 수정안 PR이 없는 시도
    (검증 실패 등)는 결과를 모르는 것이라 세지 않는다. 시도 하나는 한 번만 센다(머지가 앞선다)."""
    best: dict[int, FixOutcome] = {}
    for row in rows:
        if row.bundle != MODEL_BUNDLE or row.state not in OUTCOME_STATES:
            continue
        old = best.get(row.attempt_id)
        if old is None or _STATE_RANK[row.state] > _STATE_RANK[old.state]:
            best[row.attempt_id] = row
    with_adopted = with_known = without_adopted = without_known = 0
    for row in best.values():
        adopted = int(row.state == "merged")
        if _has_references(row.references_json):
            with_adopted, with_known = with_adopted + adopted, with_known + 1
        else:
            without_adopted, without_known = without_adopted + adopted, without_known + 1
    return Outcomes(with_adopted, with_known), Outcomes(without_adopted, without_known)


def worse(evidence: Evidence, min_attempts: int, min_baseline: int) -> bool:
    """검색을 붙인 쪽의 채택률이 낮다. 붙인 쪽이 min_attempts건, 붙이지 않은 쪽이 min_baseline건
    이상일 때만 본다(견줄 쪽이 비었거나 적으면 근거가 없어 끄지 않는다). 같으면 낮지 않다. 곱으로
    견준다."""
    w, wo = evidence.with_refs, evidence.without_refs
    if w.known < min_attempts or wo.known < max(1, min_baseline):
        return False
    return w.adopted * wo.known < wo.adopted * w.known


@dataclass(frozen=True)
class RagSwitch:
    """한 검사의 검색 상태와 그 이유. 결과 코멘트에 숫자로 보인다.

    wanted는 설정과 숫자가 정한 것이고, 켜야 하는데 선택 의존성(`pr-proof[rag]`)이 없으면
    missing_extras다. 그때 검색은 하지 않고 검사는 계속한다."""

    mode: RagMode
    wanted: bool
    why: Why
    evidence: Evidence | None = None  # auto일 때만. why가 worse면 끈 때의 숫자
    min_records: int = AUTO_MIN_RECORDS
    min_attempts: int = AUTO_MIN_ATTEMPTS
    min_baseline: int = AUTO_MIN_BASELINE
    missing_extras: bool = False

    @property
    def on(self) -> bool:
        return self.wanted and not self.missing_extras


def decide(
    mode: RagMode,
    evidence: Evidence | None,
    turned_off: Evidence | None,
    *,
    min_records: int,
    min_attempts: int,
    min_baseline: int,
) -> RagSwitch:
    """검색을 켤지 (모듈 설명의 1–4). turned_off는 이 저장소에서 auto가 같은 기준값으로 검색을
    끈 때의 숫자다. auto가 아니면 evidence는 보지 않는다."""
    limits = {"min_records": min_records, "min_attempts": min_attempts,
              "min_baseline": min_baseline}  # fmt: skip
    if mode != "auto":
        return RagSwitch(mode, mode == "on", "setting", **limits)
    if turned_off is not None:
        return RagSwitch(mode, False, "worse", turned_off, **limits)
    if evidence is None or evidence.records < min_records:
        return RagSwitch(mode, False, "few_records", evidence, **limits)
    if worse(evidence, min_attempts, min_baseline):
        return RagSwitch(mode, False, "worse", evidence, **limits)
    return RagSwitch(mode, True, "enough_records", evidence, **limits)


def _rate(o: Outcomes) -> str:
    return f"{o.adopted}/{o.known}"


def describe(switch: RagSwitch) -> str:
    """결과 코멘트의 한 줄: 지금 상태와 그 이유. 숫자만 쓰고 기록의 글은 넣지 않는다."""
    e = switch.evidence or Evidence(0)
    if switch.why == "setting":
        reason = f"`rag.enabled: {switch.mode}`"
    elif switch.why == "worse":
        reason = (
            f"자동, 검색으로 기록을 넣은 수정안의 채택률이 낮음. 넣음 {_rate(e.with_refs)}, "
            f"넣지 않음 {_rate(e.without_refs)}"
        )
    else:
        reason = f"자동, 채택·거절 기록 {e.records}/{switch.min_records}건"
        if switch.why == "enough_records":
            reason += (
                f". 수정안 채택률 넣음 {_rate(e.with_refs)}, 넣지 않음 {_rate(e.without_refs)}. "
                f"넣음 {switch.min_attempts}건, 넣지 않음 {switch.min_baseline}건부터 비교"
            )
    if switch.on:
        return f"검색: 켜짐 ({reason})"
    text = f"검색: 꺼짐 ({reason})"
    if switch.missing_extras:
        return text + ". 켜야 하지만 선택 의존성 `pr-proof[rag]`이 설치되지 않아 검색 없이 검사함"
    if switch.why == "worse":
        return text + ". 다시 켜려면 `rag.enabled: on`"
    return text


# 체크포인트에서 되살릴 수 있게 graph.py가 등록하는 타입
STATE_TYPES: Final = (
    ("worker.rag_switch_pure", "RagSwitch"),
    ("worker.rag_switch_pure", "Evidence"),
    ("worker.rag_switch_pure", "Outcomes"),
)
