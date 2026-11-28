import threading
from types import SimpleNamespace

import pytest

from app.worker.flow_state import keep_alive
from app.worker.graph import timed


class _Times:
    def __init__(self) -> None:
        self.saved: list[tuple[int, str, float]] = []

    def add_stage_time(self, run_id: int, stage: str, seconds: float) -> None:
        self.saved.append((run_id, stage, seconds))


def _clock(*ticks: float):  # noqa: ANN202
    values = iter(ticks)
    return lambda: next(values)


def test_timed_passes_the_result_through_and_saves_the_stage_time() -> None:
    store = _Times()
    ctx = SimpleNamespace(store=store, run_id=7)
    node = timed(ctx, "test", lambda state: {"seen": state["x"]}, clock=_clock(10.0, 12.5))
    assert node({"x": 1}) == {"seen": 1}
    assert store.saved == [(7, "test", 2.5)]


def test_timed_saves_the_time_of_a_stage_that_fails() -> None:
    store = _Times()
    ctx = SimpleNamespace(store=store, run_id=7)

    def broken(state: object) -> None:
        raise RuntimeError("docker가 없음")

    with pytest.raises(RuntimeError):
        timed(ctx, "checkout", broken, clock=_clock(0.0, 3.0))({})
    assert store.saved == [(7, "checkout", 3.0)]


class _BrokenTimes:
    def add_stage_time(self, run_id: int, stage: str, seconds: float) -> None:
        raise OSError("database is locked")


def test_a_failure_to_save_the_time_does_not_change_the_result_of_the_stage() -> None:
    ctx = SimpleNamespace(store=_BrokenTimes(), run_id=7)
    node = timed(ctx, "test", lambda state: {"ok": True}, clock=_clock(0.0, 1.0))
    assert node({}) == {"ok": True}


def test_a_failure_to_save_the_time_does_not_replace_the_error_of_the_stage() -> None:
    ctx = SimpleNamespace(store=_BrokenTimes(), run_id=7)

    def broken(state: object) -> None:
        raise RuntimeError("docker가 없음")

    with pytest.raises(RuntimeError, match="docker가 없음"):
        timed(ctx, "checkout", broken, clock=_clock(0.0, 3.0))({})


class _Beats:
    def __init__(self, fail: bool = False) -> None:
        self.beats: list[int] = []
        self.fail = fail
        self.seen = threading.Event()

    def heartbeat(self, run_id: int) -> None:
        self.beats.append(run_id)
        self.seen.set()
        if self.fail:
            raise OSError("database is locked")


def test_keep_alive_leaves_heartbeats_while_a_long_step_runs() -> None:
    store = _Beats()
    with keep_alive(SimpleNamespace(store=store, run_id=7), seconds=0.01):
        assert store.seen.wait(5)
    count = len(store.beats)
    assert count >= 1 and set(store.beats) == {7}
    store.seen.clear()
    assert not store.seen.wait(0.05) and len(store.beats) == count  # 블록을 나가면 멈춘다


def test_keep_alive_failure_does_not_stop_the_step() -> None:
    store = _Beats(fail=True)
    with keep_alive(SimpleNamespace(store=store, run_id=7), seconds=0.01):
        assert store.seen.wait(5)
