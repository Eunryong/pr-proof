from types import SimpleNamespace

import pytest

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
