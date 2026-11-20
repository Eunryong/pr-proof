"""검색을 켤지 정하는 워커 쪽 (`memory.switch_for`, `memory.missing_extras`, DESIGN.md 6.7).

SQLite만 쓴다. chromadb가 없는 설치에서도 돈다. 기준값은 상수를 바꾸지 않고 인자로 준다.
"""

import sys
from pathlib import Path
from typing import Final

import fakeredis
import pytest

from app.models.rag_switch import Evidence, Outcomes
from app.worker import memory
from app.worker.state import Store

from .rag_helpers import fix_outcomes, record

REPO: Final = "o/r"
HEAVY: Final = ("chromadb", "onnxruntime", "tokenizers", "numpy")
LIMITS: Final = {"min_records": 20, "min_attempts": 30, "min_baseline": 10}


def _store(path: Path) -> Store:
    return Store(path / "pipeline.db", fakeredis.FakeStrictRedis())


def _records(store: Store, count: int, repo: str = REPO) -> None:
    for n in range(count):
        store.add_record(record("adopted" if n % 2 else "rejected", n, repo=repo))


def _never() -> bool:
    raise AssertionError("검색을 켜지 않을 때는 선택 의존성을 찾지 않는다")


def _present() -> bool:
    return False


def test_auto_below_twenty_reads_sqlite_only(tmp_path: Path, monkeypatch) -> None:
    for name in HEAVY:
        monkeypatch.setitem(sys.modules, name, None)  # import하면 실패한다
    store = _store(tmp_path)
    _records(store, 19)
    _records(store, 30, repo="o/other")  # 다른 저장소의 기록은 세지 않는다
    switch = memory.switch_for(store, REPO, "auto", extras_missing=_never, **LIMITS)
    assert (switch.on, switch.why, switch.evidence) == (False, "few_records", Evidence(19))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["pipeline.db"]


def test_auto_turns_on_at_twenty(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _records(store, 20)
    switch = memory.switch_for(store, REPO, "auto", extras_missing=_present, **LIMITS)
    assert (switch.on, switch.why) == (True, "enough_records")


def test_thresholds_are_arguments(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _records(store, 3)
    on = memory.switch_for(store, REPO, "auto", min_records=3, min_attempts=30,
                           extras_missing=_present)  # fmt: skip
    assert on.on and on.min_records == 3


def test_auto_turns_off_once_and_stays_off_across_restarts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _records(store, 25)
    nxt = fix_outcomes(store, 1, 30, adopted=9, references="[1]")
    nxt = fix_outcomes(store, nxt, 30, adopted=10, references=None)
    off = memory.switch_for(store, REPO, "auto", extras_missing=_never, **LIMITS)
    assert (off.on, off.why) == (False, "worse")
    assert off.evidence == Evidence(25, Outcomes(9, 30), Outcomes(10, 30))

    # 끈 뒤 검색 없는 쪽의 채택률이 떨어져도(검색 쪽은 더 늘지 않는다) 다시 켜지 않는다.
    # 새 Store는 워커를 다시 띄운 것과 같다
    fix_outcomes(store, nxt, 30, adopted=0, references=None)
    restarted = _store(tmp_path)
    again = memory.switch_for(restarted, REPO, "auto", extras_missing=_never, **LIMITS)
    assert (again.on, again.why, again.evidence) == (False, "worse", off.evidence)


def test_threshold_change_reevaluates_with_current_numbers(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _records(store, 25)
    nxt = fix_outcomes(store, 1, 30, adopted=9, references="[1]")
    fix_outcomes(store, nxt, 30, adopted=10, references=None)
    assert memory.switch_for(store, REPO, "auto", extras_missing=_never, **LIMITS).why == "worse"
    # 기준값 31에서는 검색 쪽이 30건이라 비교하지 않는다
    changed = memory.switch_for(store, REPO, "auto", min_records=20, min_attempts=31,
                                extras_missing=_present)  # fmt: skip
    assert (changed.on, changed.why) == (True, "enough_records")


def test_equal_rates_or_a_small_comparison_group_keep_it_on(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _records(store, 25)
    nxt = fix_outcomes(store, 1, 30, adopted=10, references="[1]")
    nxt = fix_outcomes(store, nxt, 30, adopted=10, references=None)
    assert memory.switch_for(store, REPO, "auto", extras_missing=_present, **LIMITS).on

    other = _store(tmp_path / "small")
    _records(other, 25)
    nxt = fix_outcomes(other, 1, 30, adopted=0, references="[1]")
    nxt = fix_outcomes(other, nxt, 9, adopted=9, references=None)  # 견줄 쪽 9건
    switch = memory.switch_for(other, REPO, "auto", extras_missing=_present, **LIMITS)
    assert (switch.on, switch.why) == (True, "enough_records")
    assert other.rag_turned_off(REPO, 30, 10) is None
    fix_outcomes(other, nxt, 1, adopted=1, references=None)  # 10건이 되면 견준다
    switch = memory.switch_for(other, REPO, "auto", extras_missing=_never, **LIMITS)
    assert (switch.on, switch.why) == (False, "worse")


@pytest.mark.parametrize("mode", ["on", "off"])
def test_on_and_off_read_nothing_and_write_nothing(tmp_path: Path, mode: str) -> None:
    store = _store(tmp_path)
    nxt = fix_outcomes(store, 1, 30, adopted=0, references="[1]")
    fix_outcomes(store, nxt, 30, adopted=30, references=None)
    switch = memory.switch_for(store, REPO, mode, extras_missing=_present, **LIMITS)
    assert (switch.on, switch.why, switch.evidence) == (mode == "on", "setting", None)
    assert store.rag_turned_off(REPO, 30, 10) is None


def test_missing_extras_keeps_checking_without_search(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _records(store, 20)
    switch = memory.switch_for(store, REPO, "auto", extras_missing=lambda: True, **LIMITS)
    assert (switch.wanted, switch.on, switch.missing_extras) == (True, False, True)
    on = memory.switch_for(store, REPO, "on", extras_missing=lambda: True, **LIMITS)
    assert (on.on, on.missing_extras) == (False, True)


def test_missing_extras_finds_without_importing(monkeypatch) -> None:
    assert memory.missing_extras(("json",)) is False
    assert memory.missing_extras(("json", "no_such_module_for_pr_proof")) is True
    monkeypatch.setitem(sys.modules, "json_blocked_for_test", None)
    assert memory.missing_extras(("json_blocked_for_test",)) is True
    monkeypatch.delitem(sys.modules, "this", raising=False)
    assert memory.missing_extras(("this",)) is False
    assert "this" not in sys.modules  # 찾기만 하고 import하지 않았다
