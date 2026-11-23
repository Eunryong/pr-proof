"""작업 등록: 최신 head 기록(닫힘 소식이면 닫힘 표시), delivery·작업 키 중복 방지, RQ 큐 등록."""

from typing import Final, Literal

from redis import Redis
from rq import Queue, Retry

from app.shared import state_keys_pure
from app.shared.job_pure import PRJob

WORKER_FUNCTION: Final = "app.worker.main.handle"
# 환경 오류는 워커가 예외를 올려 이 간격으로 다시 시도한다. 상한이 있어 무한 반복하지 않는다
RETRY_INTERVALS: Final = (60, 300, 900)
JOB_TIMEOUT_SECONDS: Final = 30 * 60

EnqueueResult = Literal["queued", "duplicate"]


def seen_delivery(redis: Redis, delivery_id: str) -> bool:
    """이미 받은 delivery면 True. 처음이면 기록하고 False."""
    key = state_keys_pure.delivery(delivery_id)
    return not redis.set(key, "1", nx=True, ex=state_keys_pure.DELIVERY_TTL_SECONDS)


def clear_job(redis: Redis, job: PRJob) -> None:
    """죽은 워커가 남긴 작업 키를 지운다. 동기화가 생존 확인이 끊긴 작업을 다시 넣을 때 쓴다."""
    redis.delete(state_keys_pure.job(job.dedup_key))


def enqueue(redis: Redis, job: PRJob) -> EnqueueResult:
    """작업을 큐에 넣는다. 같은 작업이 대기·실행 중이면 넣지 않는다."""
    if job.kind == "update" and job.head_sha:
        # 낡은 작업 확인용. 같은 작업이 이미 있어도 최신 head는 갱신한다
        redis.set(state_keys_pure.latest_head(job.repo, job.pr_number), job.head_sha)
    elif job.kind == "close" and job.source == "webhook":
        # PR이 닫혔다는 소식이다. 워커는 하나라 닫힘 작업은 돌고 있는 검사가 끝난 뒤에야 돈다.
        # 그래서 여기서 바로 표시해, 돌고 있는 검사가 다음 확인에서 멈추게 한다(테스트와 모델
        # 호출을 더 쓰지 않는다). 동기화가 넣은 닫힘 작업은 표시하지 않는다: 열린 PR 목록이 잘려
        # 열린 PR이 빠졌을 수 있고, 워커가 GitHub에서 확인한 뒤에 표시한다
        redis.set(state_keys_pure.latest_head(job.repo, job.pr_number), state_keys_pure.CLOSED_HEAD)
    if not redis.set(
        state_keys_pure.job(job.dedup_key), "1", nx=True, ex=state_keys_pure.JOB_TTL_SECONDS
    ):
        return "duplicate"
    Queue(state_keys_pure.QUEUE_NAME, connection=redis).enqueue(
        WORKER_FUNCTION,
        job.model_dump_json(),
        retry=Retry(max=len(RETRY_INTERVALS), interval=list(RETRY_INTERVALS)),
        job_timeout=JOB_TIMEOUT_SECONDS,
    )
    return "queued"
