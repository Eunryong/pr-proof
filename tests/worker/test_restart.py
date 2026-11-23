"""재시작으로 끊긴 작업이 다시 도는가 (DESIGN F2 "끝난 상태별 재등록 규칙").

워커 프로세스가 작업 도중 죽으면 남는 것: `running`인 run(생존 확인은 테스트 실행 중에만 갱신되므로
그 전에 끊기면 started_at 그대로), 작업 키(6시간 TTL), 최신 head 키, RQ의 started 목록에 남은 작업.
웹 쪽 동기화는 runs와 작업 키만 본다.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import pytest
from rq import Queue, SimpleWorker
from rq.job import JobStatus
from rq.registry import StartedJobRegistry
from sqlmodel import Session, select

from app.api import queue, sync
from app.api.github import OpenPR
from app.api.history import History
from app.shared import state_keys_pure
from app.shared.db_pure import Run
from app.shared.job_pure import PRJob
from app.worker import main

from .helpers import HEAD, PASSED, use_runner

HEARTBEAT_TIMEOUT: Final = timedelta(minutes=20)  # sync.heartbeat_timeout_minutes 기본값


@pytest.fixture
def env(redis_env):
    """이 파일은 큐 등록·동기화·RQ를 거치므로 실제 Redis를 쓴다 (tests/conftest.py)."""
    return redis_env


def _webhook_job() -> PRJob:
    return PRJob(
        repo="o/r", pr_number=1, kind="update", head_sha=HEAD, base_sha="b" * 40,
        source="webhook",
    )  # fmt: skip


def _sync(env, now: datetime) -> list[tuple[str, int, str]]:
    pr = OpenPR(number=1, head_sha=HEAD, base_sha="b" * 40, head_ref="f", draft=False)
    return sync.run_once(
        env["deps"].settings, env["redis"], History(Path(env["deps"].store.engine.url.database)),
        list_prs=lambda repo: [pr], head_time=lambda repo, sha: "2026-11-18T00:00:00Z",
        list_comments=lambda repo, n, since: [], now=now,
    ).enqueued  # fmt: skip


def _rq_queue(env) -> Queue:
    return Queue(state_keys_pure.QUEUE_NAME, connection=env["redis"])


def _cut_off_by_restart(env) -> tuple[datetime, object]:
    """웹훅이 넣은 작업을 워커가 꺼내 run을 시작한 뒤 프로세스가 죽는다. (started_at, RQ 작업)"""
    assert queue.enqueue(env["redis"], _webhook_job()) == "queued"
    worker = SimpleWorker([_rq_queue(env)], connection=env["redis"])
    rq_job, _ = worker.dequeue_job_and_maintain_ttl(timeout=1)
    worker.prepare_execution(rq_job)
    worker.prepare_job_execution(rq_job)
    env["deps"].store.start_run(_webhook_job())
    (run,) = _runs(env)
    assert (run.state, run.heartbeat_at) == ("running", run.started_at)
    return run.started_at.replace(tzinfo=UTC), rq_job


def _runs(env) -> list[Run]:
    with Session(env["deps"].store.engine) as session:
        return list(session.exec(select(Run).order_by(Run.id)))


def test_job_cut_off_by_a_restart_runs_again_at_the_first_sync_after_the_timeout(env) -> None:
    use_runner(env, lambda *a, **k: PASSED)
    started, rq_job = _cut_off_by_restart(env)
    job_key = state_keys_pure.job(_webhook_job().dedup_key)
    assert env["redis"].get(job_key) is not None
    assert rq_job.get_status() == JobStatus.STARTED
    assert _rq_queue(env).jobs == []

    # 생존 확인이 20분 끊기기 전에는 아직 도는 작업과 구별하지 않는다
    assert _sync(env, started + HEARTBEAT_TIMEOUT - timedelta(seconds=1)) == []
    assert _rq_queue(env).jobs == []

    assert _sync(env, started + HEARTBEAT_TIMEOUT) == [("o/r", 1, "dead_worker")]
    (requeued,) = _rq_queue(env).jobs
    payload = requeued.args[0]
    assert PRJob.model_validate_json(payload).source == "sync"

    assert main.handle(payload, env["deps"]) == "passed"
    assert [(r.state, r.source) for r in _runs(env)] == [
        ("running", "webhook"),
        ("done", "sync"),
    ]
    assert env["redis"].get(job_key) is None
    assert _sync(env, started + timedelta(hours=1)) == []


def test_abandoned_rq_job_also_comes_back_after_the_job_timeout(env) -> None:
    """RQ도 started 목록의 작업을 job_timeout(30분)+60초 뒤 버려진 작업으로 보고 재시도 간격으로
    다시 넣는다. 동기화가 20분에 넣은 작업이 이미 돌았으면 같은 head를 한 번 더 검사한다."""
    use_runner(env, lambda *a, **k: PASSED)
    started, rq_job = _cut_off_by_restart(env)
    registry = StartedJobRegistry(queue=_rq_queue(env))

    registry.cleanup(timestamp=started.timestamp() + queue.JOB_TIMEOUT_SECONDS)
    assert registry.get_job_ids() == [rq_job.id]
    registry.cleanup(timestamp=started.timestamp() + queue.JOB_TIMEOUT_SECONDS + 61)
    assert registry.get_job_ids() == []
    assert rq_job.get_status(refresh=True) == JobStatus.SCHEDULED

    assert _sync(env, started + HEARTBEAT_TIMEOUT) == [("o/r", 1, "dead_worker")]
    (requeued,) = _rq_queue(env).jobs
    assert main.handle(requeued.args[0], env["deps"]) == "passed"
    assert main.handle(rq_job.args[0], env["deps"]) == "passed"
    assert [(r.state, r.source) for r in _runs(env)] == [
        ("running", "webhook"),
        ("done", "sync"),
        ("done", "webhook"),
    ]


def test_run_marked_cancelled_by_hand_is_not_requeued_while_the_job_key_lives(env) -> None:
    """손으로 `cancelled`로 바꾸면 동기화는 바로 다시 넣으려 하지만 죽은 워커의 작업 키가 남아
    있어 중복으로 보고 넣지 않는다. 키를 지우는 것은 `running`이 20분 끊겼을 때뿐이다."""
    started, _ = _cut_off_by_restart(env)
    (run,) = _runs(env)
    env["deps"].store.finish_run(run.id, "cancelled")

    assert _sync(env, started + timedelta(minutes=2)) == []
    assert _sync(env, started + timedelta(hours=5)) == []
    assert _rq_queue(env).jobs == []

    env["redis"].delete(state_keys_pure.job(_webhook_job().dedup_key))  # TTL 6시간이 지난 자리
    assert _sync(env, started + timedelta(hours=6)) == [("o/r", 1, "cancelled")]
