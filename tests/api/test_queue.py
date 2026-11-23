import fakeredis
import pytest
from rq import Queue

from app.api import queue
from app.shared import state_keys_pure
from app.shared.job_pure import PRJob


@pytest.fixture
def redis():
    return fakeredis.FakeStrictRedis()


def _job(head: str = "abc", kind: str = "update") -> PRJob:
    return PRJob(repo="o/r", pr_number=1, kind=kind, head_sha=head, base_sha="b", source="webhook")


def _queued(redis) -> int:
    return len(Queue(state_keys_pure.QUEUE_NAME, connection=redis))


def test_enqueue_records_latest_head_and_queues(redis) -> None:
    assert queue.enqueue(redis, _job("abc")) == "queued"
    assert redis.get(state_keys_pure.latest_head("o/r", 1)) == b"abc"
    assert _queued(redis) == 1


def test_same_job_is_not_queued_twice(redis) -> None:
    queue.enqueue(redis, _job("abc"))
    assert queue.enqueue(redis, _job("abc")) == "duplicate"
    assert _queued(redis) == 1


def test_new_head_is_queued_and_moves_latest_head(redis) -> None:
    queue.enqueue(redis, _job("abc"))
    assert queue.enqueue(redis, _job("def")) == "queued"
    assert redis.get(state_keys_pure.latest_head("o/r", 1)) == b"def"
    assert _queued(redis) == 2


def test_close_webhook_marks_the_pr_closed_at_once(redis) -> None:
    """닫힘 소식을 받으면 바로 닫힘 표시를 적는다. 워커는 하나라 닫힘 작업은 돌고 있는 검사가 끝난
    뒤에야 돌고, 표시가 그때 적히면 돌던 검사가 끝까지 간다. 닫힘 작업의 커밋으로 덮지는 않는다."""
    queue.enqueue(redis, _job("abc"))
    assert queue.enqueue(redis, _job("zzz", kind="close")) == "queued"
    latest = redis.get(state_keys_pure.latest_head("o/r", 1))
    assert latest == state_keys_pure.CLOSED_HEAD.encode() and latest not in (b"abc", b"zzz")


def test_close_job_from_sync_does_not_mark_closed(redis) -> None:
    """동기화가 넣은 닫힘 작업은 표시하지 않는다. 열린 PR 목록이 잘려 열린 PR이 빠졌을 수 있어,
    워커가 GitHub에서 확인한 뒤에 표시한다."""
    queue.enqueue(redis, _job("abc"))
    queue.enqueue(redis, PRJob(repo="o/r", pr_number=1, kind="close", source="sync"))
    assert redis.get(state_keys_pure.latest_head("o/r", 1)) == b"abc"


def test_reopen_after_close_moves_latest_head_back(redis) -> None:
    queue.enqueue(redis, _job("abc"))
    queue.enqueue(redis, _job("abc", kind="close"))
    queue.enqueue(redis, _job("def"))  # reopened나 새 커밋의 소식
    assert redis.get(state_keys_pure.latest_head("o/r", 1)) == b"def"


def test_queued_job_has_bounded_retry(redis) -> None:
    queue.enqueue(redis, _job())
    rq_job = Queue(state_keys_pure.QUEUE_NAME, connection=redis).jobs[0]
    assert rq_job.retries_left == len(queue.RETRY_INTERVALS)


def test_seen_delivery(redis) -> None:
    assert not queue.seen_delivery(redis, "d-1")
    assert queue.seen_delivery(redis, "d-1")
