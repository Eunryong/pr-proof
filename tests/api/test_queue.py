import fakeredis
import pytest
from rq import Queue

from api import queue
from shared import state_keys_pure as state_keys
from shared.job_pure import PRJob


@pytest.fixture
def redis():
    return fakeredis.FakeStrictRedis()


def _job(head: str = "abc", kind: str = "update") -> PRJob:
    return PRJob(repo="o/r", pr_number=1, kind=kind, head_sha=head, base_sha="b", source="webhook")


def _queued(redis) -> int:
    return len(Queue(state_keys.QUEUE_NAME, connection=redis))


def test_enqueue_records_latest_head_and_queues(redis) -> None:
    assert queue.enqueue(redis, _job("abc")) == "queued"
    assert redis.get(state_keys.latest_head("o/r", 1)) == b"abc"
    assert _queued(redis) == 1


def test_same_job_is_not_queued_twice(redis) -> None:
    queue.enqueue(redis, _job("abc"))
    assert queue.enqueue(redis, _job("abc")) == "duplicate"
    assert _queued(redis) == 1


def test_new_head_is_queued_and_moves_latest_head(redis) -> None:
    queue.enqueue(redis, _job("abc"))
    assert queue.enqueue(redis, _job("def")) == "queued"
    assert redis.get(state_keys.latest_head("o/r", 1)) == b"def"
    assert _queued(redis) == 2


def test_close_does_not_move_latest_head(redis) -> None:
    queue.enqueue(redis, _job("abc"))
    queue.enqueue(redis, _job("zzz", kind="close"))
    assert redis.get(state_keys.latest_head("o/r", 1)) == b"abc"


def test_queued_job_has_bounded_retry(redis) -> None:
    queue.enqueue(redis, _job())
    rq_job = Queue(state_keys.QUEUE_NAME, connection=redis).jobs[0]
    assert rq_job.retries_left == len(queue.RETRY_INTERVALS)


def test_seen_delivery(redis) -> None:
    assert not queue.seen_delivery(redis, "d-1")
    assert queue.seen_delivery(redis, "d-1")
