"""작업 등록: 최신 head 기록, delivery·작업 키 중복 방지, RQ 큐 등록."""

from typing import Final, Literal

from redis import Redis
from rq import Queue, Retry

from app.models import state_keys
from app.models.job import PRJob

WORKER_FUNCTION: Final = "app.worker.main.handle"
# 환경 오류는 워커가 예외를 올려 이 간격으로 다시 시도한다. 상한이 있어 무한 반복하지 않는다
RETRY_INTERVALS: Final = (60, 300, 900)
JOB_TIMEOUT_SECONDS: Final = 30 * 60

EnqueueResult = Literal["queued", "duplicate"]


def seen_delivery(redis: Redis, delivery_id: str) -> bool:
    """이미 받은 delivery면 True. 처음이면 기록하고 False."""
    key = state_keys.delivery(delivery_id)
    return not redis.set(key, "1", nx=True, ex=state_keys.DELIVERY_TTL_SECONDS)


def enqueue(redis: Redis, job: PRJob) -> EnqueueResult:
    """작업을 큐에 넣는다. 같은 작업이 대기·실행 중이면 넣지 않는다."""
    if job.kind == "update" and job.head_sha:
        # 낡은 작업 확인용. 같은 작업이 이미 있어도 최신 head는 갱신한다
        redis.set(state_keys.latest_head(job.repo, job.pr_number), job.head_sha)
    if not redis.set(state_keys.job(job.dedup_key), "1", nx=True, ex=state_keys.JOB_TTL_SECONDS):
        return "duplicate"
    Queue(state_keys.QUEUE_NAME, connection=redis).enqueue(
        WORKER_FUNCTION,
        job.model_dump_json(),
        retry=Retry(max=len(RETRY_INTERVALS), interval=list(RETRY_INTERVALS)),
        job_timeout=JOB_TIMEOUT_SECONDS,
    )
    return "queued"
