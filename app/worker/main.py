"""진입점: RQ 워커. `python -m app.worker.main`.

큐의 작업 하나를 handle()이 종류별로 나눠 처리한다 (update.py, command.py). 환경 오류는 PR에
실패로 남기지 않고 예외를 올려 RQ가 정해진 간격으로 다시 시도하게 한다 (retry.py, 상한 있음).
"""

import logging
import sys

from redis import Redis
from rq import Queue, SimpleWorker

from app import config
from app.models import state_keys
from app.models.job import PRJob
from app.models.ops_alert import Alert
from app.notify import slack
from app.worker import command, preflight, retry, update
from app.worker.deps import Deps

log = logging.getLogger(__name__)


def handle(payload: str, deps: Deps | None = None) -> str:
    job = PRJob.model_validate_json(payload)
    deps = deps or Deps.from_config()
    try:
        if job.kind == "update":
            result = update.process_update(job, deps)
        elif job.kind == "command":
            result = command.process_command(job, deps)
        else:
            result = update.process_close(job, deps)
    except Exception as error:
        # 마지막 시도만 알린다. 앞 시도의 실패는 RQ가 다시 하고 PR에 대기 중으로 보인다
        if retry.attempt()[1]:
            kind = (
                "env_error_final"
                if isinstance(error, retry.EnvironmentFailure)
                else "pipeline_error"
            )
            slack.send(Alert(kind, job.repo, job.pr_number), deps.store.redis)
        raise
    deps.store.release(job)
    return result


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = config.load_config()
    redis = Redis.from_url(settings.redis_url)
    problems = preflight.failures(preflight.run_checks(redis))
    if problems:
        for problem in problems:
            log.error(problem)
        slack.send(Alert("preflight_failed"), redis)
        return 1
    # 동시 작업 1개. macOS에서 fork 문제를 피하려고 작업을 같은 프로세스에서 실행한다
    worker = SimpleWorker([Queue(state_keys.QUEUE_NAME, connection=redis)], connection=redis)
    worker.work(with_scheduler=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
