from datetime import UTC, datetime, timedelta
from typing import Final

import pytest

from app.models.sync_rules import RunSnapshot, enqueue_reason

NOW: Final = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
BACKOFF: Final = timedelta(minutes=30)
HEARTBEAT: Final = timedelta(minutes=20)


def _run(state: str, *, ago: int, finished: bool = True) -> RunSnapshot:
    at = NOW - timedelta(minutes=ago)
    return RunSnapshot(state=state, heartbeat_at=at, finished_at=at if finished else None)


def _reason(*runs: RunSnapshot):
    return enqueue_reason(runs, NOW, env_error_backoff=BACKOFF, heartbeat_timeout=HEARTBEAT)


@pytest.mark.parametrize(
    ("runs", "reason"),
    [
        ((), "no_record"),
        ((_run("done", ago=500),), None),  # 결과가 있으면 새 커밋이 올 때만
        ((_run("env_error", ago=10),), None),  # 30분이 안 지남
        ((_run("env_error", ago=30),), "env_error_backoff"),
        ((_run("running", ago=5, finished=False),), None),  # 살아 있음
        ((_run("running", ago=20, finished=False),), "dead_worker"),
        ((_run("cancelled", ago=1),), "cancelled"),
        ((_run("done", ago=60), _run("env_error", ago=45)), None),  # done이 하나라도 있으면
        ((_run("env_error", ago=90), _run("running", ago=1, finished=False)), None),
        ((_run("error", ago=500),), None),  # 파이프라인 오류는 새 커밋 때만
        ((_run("env_error", ago=90), _run("error", ago=60)), None),
    ],
    ids=[
        "no-record", "done", "env-error-recent", "env-error-old", "running-alive",
        "running-dead", "cancelled", "done-then-env-error", "env-error-then-running",
        "error", "env-error-then-error",
    ],
)  # fmt: skip
def test_enqueue_reason(runs, reason) -> None:
    assert _reason(*runs) == reason


def test_naive_timestamps_from_sqlite_are_utc() -> None:
    naive = (NOW - timedelta(minutes=31)).replace(tzinfo=None)
    run = RunSnapshot(state="env_error", heartbeat_at=naive, finished_at=naive)
    assert _reason(run) == "env_error_backoff"
