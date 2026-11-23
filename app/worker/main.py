"""진입점: RQ 워커. `python -m app.worker.main`.

큐의 작업 하나를 handle()이 종류별로 나눠 처리한다 (update.py, command.py, feedback_job.py).
환경 오류는 PR에 실패로 남기지 않고 예외를 올려 RQ가 정해진 간격으로 다시 시도하게 한다
(retry.py, 상한 있음).
"""

import logging
import sys
from collections.abc import Callable
from functools import partial

from redis import Redis
from rq import Queue, SimpleWorker
from sqlalchemy.exc import SQLAlchemyError

from app.shared import config, state_keys_pure
from app.shared.job_pure import PRJob
from app.worker import command, feedback_job, preflight, retry, update
from app.worker.deps import Deps
from app.worker.store.state import open_engine

log = logging.getLogger(__name__)


def handle(
    payload: str,
    deps: Deps | None = None,
    checks: Callable[[Redis], list[preflight.Check]] | None = None,
) -> str:
    job = PRJob.model_validate_json(payload)
    deps = deps or Deps.from_config()
    try:
        if job.kind == "update":
            result = update.process_update(job, deps)
        elif job.kind == "command":
            result = command.process_command(job, deps)
        elif job.kind == "feedback":
            result = feedback_job.process_feedback(job, deps)
        elif job.kind == "resolve":
            result = feedback_job.process_resolve(job, deps)
        else:
            result = update.process_close(job, deps)
    except Exception as error:
        # 재시도를 모두 쓴 환경 오류만 시작 전 확인으로 다시 가려 알린다. 앞 시도의 실패는 RQ가
        # 다시 하고, 다시 해서 풀리는 오류는 알리지 않는다. 확인이 모두 통과하면 알리지 않는다
        if isinstance(error, retry.EnvironmentFailure) and retry.attempt()[1]:
            run_checks = checks or partial(preflight.run_checks, backend=deps.backend)
            preflight.alert(run_checks(deps.store.redis), deps.settings.data_dir)
        raise
    deps.store.release(job)
    return result


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = config.load_config()
    # 웹 쪽 동기화는 읽기만 한다. 배포가 더한 테이블과 열을 작업이 오기 전에 만들어 둔다
    try:
        open_engine(settings.db_path).dispose()
    except (OSError, SQLAlchemyError, RuntimeError):
        log.exception("DB 스키마를 맞추지 못해 워커를 시작하지 않는다: %s", settings.db_path)
        return 1
    redis = Redis.from_url(settings.redis_url)
    checks = preflight.run_checks(redis)
    problems = preflight.failures(checks)
    if problems:
        for problem in problems:
            log.error(problem)
        preflight.alert(checks, settings.data_dir)
        return 1
    # 동시 작업 1개. macOS에서 fork 문제를 피하려고 작업을 같은 프로세스에서 실행한다
    worker = SimpleWorker([Queue(state_keys_pure.QUEUE_NAME, connection=redis)], connection=redis)
    worker.work(with_scheduler=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
