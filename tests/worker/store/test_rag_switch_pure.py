"""검색을 켜는 조건 (docs/DESIGN.md 6.7). 기준값은 상수를 바꾸지 않고 인자로 준다."""

from collections.abc import Mapping
from typing import Final

import pytest

from app.worker.config.repo_config_pure import parse_mode
from app.worker.store.rag_switch_pure import (
    AUTO_MIN_ATTEMPTS,
    AUTO_MIN_BASELINE,
    AUTO_MIN_RECORDS,
    Evidence,
    FixOutcome,
    Outcomes,
    RagSwitch,
    comment_line,
    decide,
    describe,
    outcomes,
    worse,
)

LIMITS: Final[Mapping[str, int]] = {"min_records": 20, "min_attempts": 30, "min_baseline": 10}


def _auto(evidence: Evidence | None, turned_off: Evidence | None = None) -> RagSwitch:
    return decide("auto", evidence, turned_off, **LIMITS)


def test_initial_values_are_twenty_thirty_and_ten() -> None:
    assert (AUTO_MIN_RECORDS, AUTO_MIN_ATTEMPTS, AUTO_MIN_BASELINE) == (20, 30, 10)


@pytest.mark.parametrize(
    ("value", "mode"),
    [("auto", "auto"), ("on", "on"), ("off", "off"), (True, "on"), (False, "off")],
)
def test_parse_mode_reads_the_three_values_and_old_booleans(value: object, mode: str) -> None:
    assert parse_mode(value) == mode


@pytest.mark.parametrize("value", ["Auto", "ON", "maybe", "", 1, 0, None, ["on"], {"a": 1}])
def test_parse_mode_rejects_everything_else(value: object) -> None:
    assert parse_mode(value) is None


@pytest.mark.parametrize(("records", "on"), [(0, False), (19, False), (20, True), (21, True)])
def test_auto_turns_on_at_twenty_records(records: int, on: bool) -> None:
    switch = _auto(Evidence(records))
    assert switch.on is on
    assert switch.why == ("enough_records" if on else "few_records")


def test_auto_with_no_evidence_stays_off() -> None:
    assert _auto(None).on is False


@pytest.mark.parametrize("mode", ["on", "off"])
def test_on_and_off_ignore_both_conditions(mode: str) -> None:
    few = Evidence(0)
    bad = Evidence(100, Outcomes(0, 50), Outcomes(50, 50))
    for evidence in (few, bad, None):
        switch = decide(mode, evidence, bad, **LIMITS)
        assert (switch.on, switch.why, switch.evidence) == (mode == "on", "setting", None)


@pytest.mark.parametrize(
    ("with_refs", "without_refs", "turns_off"),
    [
        (Outcomes(0, 29), Outcomes(30, 30), False),  # 검색 쪽이 29건이면 비교하지 않는다
        (Outcomes(0, 30), Outcomes(29, 29), True),  # 검색 쪽 30건, 견줄 쪽은 10건이면 된다
        (Outcomes(0, 30), Outcomes(10, 10), True),  # 견줄 쪽 10건 (경계)
        (Outcomes(0, 30), Outcomes(9, 9), False),  # 견줄 쪽 9건은 근거가 모자란다
        (Outcomes(0, 30), Outcomes(0, 0), False),  # 견줄 쪽이 비었다
        (Outcomes(9, 30), Outcomes(10, 30), True),  # 30건씩, 검색 쪽이 낮다
        (Outcomes(10, 30), Outcomes(10, 30), False),  # 같으면 켜 둔다
        (Outcomes(20, 40), Outcomes(15, 30), False),  # 비율이 같다 (0.5 = 0.5)
        (Outcomes(19, 40), Outcomes(15, 30), True),  # 0.475 < 0.5
        (Outcomes(11, 30), Outcomes(10, 30), False),  # 검색 쪽이 높다
    ],
)
def test_auto_turns_off_at_thirty_search_attempts_when_search_is_lower(
    with_refs: Outcomes, without_refs: Outcomes, turns_off: bool
) -> None:
    evidence = Evidence(25, with_refs, without_refs)
    assert worse(evidence, 30, 10) is turns_off
    switch = _auto(evidence)
    assert switch.on is not turns_off
    assert switch.why == ("worse" if turns_off else "enough_records")


def test_an_empty_comparison_group_never_turns_it_off_even_with_baseline_zero() -> None:
    assert worse(Evidence(25, Outcomes(0, 30), Outcomes()), 30, 0) is False


def test_comparison_is_not_looked_at_below_twenty_records() -> None:
    switch = _auto(Evidence(19, Outcomes(0, 30), Outcomes(30, 30)))
    assert (switch.on, switch.why) == (False, "few_records")


def test_turned_off_stays_off_whatever_the_numbers_say_now() -> None:
    then = Evidence(25, Outcomes(9, 30), Outcomes(10, 30))
    switch = _auto(Evidence(500, Outcomes(40, 40), Outcomes(0, 40)), turned_off=then)
    assert (switch.on, switch.why, switch.evidence) == (False, "worse", then)


def _row(attempt: int, state: str, refs: str | None, bundle: str = "model") -> FixOutcome:
    return FixOutcome(attempt, bundle, state, refs)


def test_outcomes_count_merged_and_rejected_model_fix_pull_requests() -> None:
    rows = [
        _row(1, "merged", "[3]"),
        _row(2, "rejected", "[3, 4]"),
        _row(3, "merged", "[]"),  # 검색은 했지만 기준값을 넘은 기록이 없었다
        _row(4, "rejected", None),  # 검색을 하지 않았다
        _row(5, "merged", "not json"),
        _row(6, "open", "[3]"),  # 사람이 아직 정하지 않았다
        _row(7, "superseded", "[3]"),  # 파이프라인이 닫았다
        _row(8, "closed_with_pr", None),
        _row(9, "merged", "[3]", bundle="mechanical"),  # 기계적 수정에는 검색이 없다
    ]
    assert outcomes(rows) == (Outcomes(1, 2), Outcomes(2, 3))


def test_one_attempt_is_counted_once_and_a_merge_wins() -> None:
    rows = [_row(1, "superseded", "[3]"), _row(1, "rejected", "[3]"), _row(1, "merged", "[3]")]
    assert outcomes(rows) == (Outcomes(1, 1), Outcomes(0, 0))
    assert outcomes(reversed(rows)) == (Outcomes(1, 1), Outcomes(0, 0))


def test_no_rows_is_two_empty_groups() -> None:
    assert outcomes([]) == (Outcomes(), Outcomes())


def test_describe_shows_the_state_and_numbers_only() -> None:
    assert describe(_auto(Evidence(12))) == "검색: 꺼짐 (자동, 채택·거절 기록 12/20건)"
    assert describe(_auto(Evidence(25, Outcomes(3, 10), Outcomes(5, 12)))) == (
        "검색: 켜짐 (자동, 채택·거절 기록 25/20건. 수정안 채택률 넣음 3/10, 넣지 않음 5/12. "
        "넣음 30건, 넣지 않음 10건부터 비교)"
    )
    assert describe(_auto(Evidence(25, Outcomes(9, 30), Outcomes(10, 30)))) == (
        "검색: 꺼짐 (자동, 검색으로 기록을 넣은 수정안의 채택률이 낮음. 넣음 9/30, 넣지 않음 "
        "10/30). 다시 켜려면 `rag.enabled: on`"
    )
    assert describe(decide("on", None, None, **LIMITS)) == "검색: 켜짐 (`rag.enabled: on`)"
    assert describe(decide("off", None, None, **LIMITS)) == "검색: 꺼짐 (`rag.enabled: off`)"


def test_describe_says_when_the_extras_are_missing() -> None:
    switch = RagSwitch("on", True, "setting", missing_extras=True)
    assert switch.on is False
    assert describe(switch) == (
        "검색: 꺼짐 (`rag.enabled: on`). 켜야 하지만 선택 의존성 `pr-proof[rag]`이 설치되지 않아 "
        "검색 없이 검사함. 켜려면 이 컴퓨터에서 `pr-proof rag on`"
    )


def test_machine_setting_turns_search_off_before_anything_else() -> None:
    """이 컴퓨터의 `rag.allowed: false`면 저장소의 `rag.enabled`와 숫자를 보지 않는다. 값이 없거나
    true면 지금과 같다."""
    for mode in ("auto", "on", "off"):
        switch = decide(mode, Evidence(25), None, allowed=False, **LIMITS)
        assert (switch.wanted, switch.why, switch.evidence) == (False, "machine", None)
        assert describe(switch) == "검색: 꺼짐 (이 컴퓨터에서 끔, `rag.allowed: false`)"
        assert comment_line(switch) is None  # 설정으로 끈 것과 같이 코멘트에 싣지 않는다
    for allowed in (None, True):
        assert decide("on", None, None, allowed=allowed, **LIMITS) == decide("on", None, None,
                                                                             **LIMITS)  # fmt: skip


def test_comment_shows_the_search_line_only_when_it_tells_something() -> None:
    """검색이 꺼져 있으면 결과 코멘트에 싣지 않는다. auto가 채택률이 낮아 다시 끈 것과 켜야 하는데
    선택 의존성이 없는 것은 사람이 할 일이 있어 싣는다. 켜져 있으면 늘 싣는다 (pr-proof#78)."""
    assert comment_line(_auto(Evidence(12))) is None
    assert comment_line(decide("off", None, None, **LIMITS)) is None
    on = decide("on", None, None, **LIMITS)
    assert comment_line(on) == describe(on)
    worse_ = _auto(Evidence(25, Outcomes(9, 30), Outcomes(10, 30)))
    assert comment_line(worse_) == describe(worse_)
    missing = RagSwitch("auto", True, "enough_records", Evidence(20), missing_extras=True)
    assert comment_line(missing) == describe(missing)
