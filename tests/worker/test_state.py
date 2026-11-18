from pathlib import Path

import fakeredis
import pytest

from app.models import state_keys
from app.models.job import PRJob
from app.worker.state import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "db" / "pipeline.db", fakeredis.FakeStrictRedis())


def _job(head: str = "abc") -> PRJob:
    return PRJob(
        repo="o/r", pr_number=1, kind="update", head_sha=head, base_sha="b", source="webhook"
    )


def test_is_stale_compares_latest_head(store: Store) -> None:
    assert not store.is_stale(_job())  # 기록 없음
    store.redis.set(state_keys.latest_head("o/r", 1), "abc")
    assert not store.is_stale(_job("abc"))
    store.redis.set(state_keys.latest_head("o/r", 1), "def")
    assert store.is_stale(_job("abc"))


def test_release_removes_job_key(store: Store) -> None:
    job = _job()
    store.redis.set(state_keys.job(job.dedup_key), "1")
    store.release(job)
    assert store.redis.get(state_keys.job(job.dedup_key)) is None


def test_run_lifecycle_and_last_comment(store: Store) -> None:
    assert store.last_comment_id("o/r", 1) is None
    first = store.start_run(_job())
    store.heartbeat(first)
    store.finish_run(first, "done", test_status="passed", comment_id=7)
    second = store.start_run(_job("def"))
    store.finish_run(second, "cancelled")  # 코멘트 ID 없이 끝나도
    assert store.last_comment_id("o/r", 1) == 7  # 마지막으로 단 코멘트를 기억한다
