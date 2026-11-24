from datetime import UTC, datetime, timedelta
from typing import Final

import pytest

from app.api.sync_rules_pure import RunSnapshot, enqueue_reason

NOW: Final = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
BACKOFF: Final = timedelta(minutes=30)
HEARTBEAT: Final = timedelta(minutes=20)


def _run(state: str, *, ago: int, finished: bool = True) -> RunSnapshot:
    at = NOW - timedelta(minutes=ago)
    return RunSnapshot(state=state, heartbeat_at=at, finished_at=at if finished else None)


def _reason(*runs: RunSnapshot, commit: str = ""):
    return enqueue_reason(runs, NOW, env_error_backoff=BACKOFF, heartbeat_timeout=HEARTBEAT,
                          pipeline_commit=commit)  # fmt: skip


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
        ((_run("blocked", ago=500),), None),  # 승인을 기다리는 외부 작성자의 PR (8.6)
        ((_run("env_error", ago=90), _run("blocked", ago=60)), None),
        # 같은 저장소를 보는 다른 서버가 이미 했다. 새 커밋 때만
        ((_run("elsewhere", ago=500),), None),
        ((_run("deferred", ago=500), _run("elsewhere", ago=400)), None),
        # 다른 서버를 기다린다. 30분이 지나면 한 번 더 넣는다
        ((_run("deferred", ago=29),), None),
        ((_run("deferred", ago=30),), "deferred"),
    ],
    ids=[
        "no-record", "done", "env-error-recent", "env-error-old", "running-alive",
        "running-dead", "cancelled", "done-then-env-error", "env-error-then-running",
        "error", "env-error-then-error", "blocked", "env-error-then-blocked",
        "elsewhere", "deferred-then-elsewhere", "deferred-recent", "deferred-old",
    ],
)  # fmt: skip
def test_enqueue_reason(runs, reason) -> None:
    assert _reason(*runs) == reason


def test_naive_timestamps_from_sqlite_are_utc() -> None:
    naive = (NOW - timedelta(minutes=31)).replace(tzinfo=None)
    run = RunSnapshot(state="env_error", heartbeat_at=naive, finished_at=naive)
    assert _reason(run) == "env_error_backoff"


def _by(state: str, commit: str | None, *, ago: int = 60, finished: bool = True) -> RunSnapshot:
    at = NOW - timedelta(minutes=ago)
    return RunSnapshot(state=state, heartbeat_at=at, finished_at=at if finished else None,
                       pipeline_commit=commit)  # fmt: skip


@pytest.mark.parametrize(
    ("runs", "reason"),
    [
        ((_by("done", "old"),), "new_pipeline"),  # 옛 코드가 낸 결과는 `/recheck` 없이 다시 본다
        ((_by("done", None),), "new_pipeline"),  # 커밋을 적기 전의 기록도 옛 코드의 것이다
        ((_by("done", "new"),), None),  # 지금 코드로 한 번 끝났으면 다시 넣지 않는다
        ((_by("done", "old"), _by("done", "new")), None),
        ((_by("error", "old"),), "new_pipeline"),  # 옛 코드의 버그는 새 코드로 다시 해 본다
        ((_by("error", "new"),), None),
        ((_by("done", "old"), _by("error", "new")), None),  # 지금 코드로 이미 해 봤다
        ((_by("blocked", "old"),), None),  # 승인은 코드가 바뀌어도 사람이 한다 (8.6)
        ((_by("done", "old"), _by("running", "new", ago=1, finished=False)), None),  # 도는 중
        ((_by("done", "old"), _by("env_error", "new", ago=5)), None),  # 물러나 기다리는 중
        ((_by("done", "old"), _by("env_error", "new", ago=45)), "env_error_backoff"),
    ],
    ids=[
        "done-old", "done-unknown", "done-new", "old-then-new", "error-old", "error-new",
        "old-done-then-new-error", "blocked", "rerun-running", "rerun-env-error-recent",
        "rerun-env-error-old",
    ],
)  # fmt: skip
def test_results_of_older_pipeline_code_are_checked_again(runs, reason) -> None:
    assert _reason(*runs, commit="new") == reason


def test_unknown_pipeline_commit_never_requeues_finished_results() -> None:
    """지금 코드의 커밋을 모르면(git이 없는 배포) 끝난 결과를 다시 넣지 않는다."""
    assert _reason(_by("done", "old"), commit="") is None
    assert _reason(_by("done", None), commit="") is None


def test_other_server_states_are_not_results_of_old_code() -> None:
    """다른 서버 때문에 검사하지 않은 기록은 옛 코드의 결과가 아니다. 배포 뒤 다시 넣지 않는다."""
    assert _reason(_by("elsewhere", "old"), commit="new") is None
    assert _reason(_by("deferred", "old", ago=10), commit="new") is None
