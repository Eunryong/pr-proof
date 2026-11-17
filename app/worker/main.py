"""진입점: RQ 워커. `python -m app.worker.main`.

큐의 작업 하나를 handle()이 처리한다. 환경 오류는 PR에 실패로 남기지 않고 예외를 올려
RQ가 정해진 간격으로 다시 시도하게 한다 (재시도 상한이 있어 무한 반복하지 않는다).
"""

import logging
import sys
from dataclasses import dataclass

from redis import Redis
from rq import Queue, SimpleWorker, get_current_job

from app import config
from app.models import state_keys
from app.models.install import install_failure_outcome
from app.models.job import PRJob
from app.sandbox.docker_cli import SandboxError
from app.sandbox.venv import DependencyInstallError
from app.worker import graph, preflight, report
from app.worker.github import GitHub, GitHubError
from app.worker.repo import GitError
from app.worker.state import Store

log = logging.getLogger(__name__)

EnvironmentFailure = (SandboxError, GitError, GitHubError)


@dataclass
class Deps:
    store: Store
    github: GitHub
    settings: config.PipelineConfig

    @classmethod
    def from_config(cls) -> "Deps":
        settings = config.load_config()
        redis = Redis.from_url(settings.redis_url)
        return cls(Store(settings.db_path, redis), GitHub(), settings)


def _attempt() -> tuple[int, bool]:
    """(이번 시도 번호, 마지막 시도인지). RQ 밖에서 부르면 (1, True)."""
    current = get_current_job()
    if current is None or current.retries_left is None:
        return 1, True
    total = len(current.retry_intervals or []) or 0
    attempt = total - current.retries_left + 1
    return attempt, current.retries_left == 0


def handle(payload: str, deps: Deps | None = None) -> str:
    job = PRJob.model_validate_json(payload)
    deps = deps or Deps.from_config()
    if job.kind == "update":
        result = process_update(job, deps)
    elif job.kind == "close":
        result = "closed"  # 1단계는 작업마다 worktree를 지우므로 정리할 것이 없다
    else:
        result = "command_not_supported"  # 명령 처리는 2단계 (/recheck, /fix)
    deps.store.release(job)
    return result


def process_update(job: PRJob, deps: Deps) -> str:
    store, github = deps.store, deps.github
    if store.is_stale(job):
        return "stale"  # 더 새 커밋의 작업이 있다
    run_id = store.start_run(job)
    comment_id = store.last_comment_id(job.repo, job.pr_number)
    attempt, final = _attempt()
    ctx = graph.FlowContext(
        store=store,
        github=github,
        data_dir=deps.settings.data_dir,
        run_id=run_id,
        comment_id=comment_id,
    )
    try:
        ctx.comment_id = github.upsert_comment(
            job.repo, job.pr_number, report.render_progress(job.head_sha, "체크아웃"), comment_id
        )
        result = graph.run_flow(job, ctx)
    except DependencyInstallError as e:
        if install_failure_outcome(e.kind, attempt) == "unrunnable":
            ctx.comment_id = github.upsert_comment(
                job.repo, job.pr_number, report.render_install_failure(job.head_sha, e.detail),
                ctx.comment_id,
            )  # fmt: skip
            store.finish_run(run_id, "done", test_status="unrunnable", comment_id=ctx.comment_id)
            return "unrunnable"
        _give_up_or_retry(job, ctx, run_id, final, e)
        raise
    except EnvironmentFailure as e:
        _give_up_or_retry(job, ctx, run_id, final, e)
        raise

    if result.get("stopped"):
        store.finish_run(run_id, "cancelled", comment_id=ctx.comment_id)
        return "stale"  # 새 커밋의 작업이 코멘트를 이어서 고친다
    run = result["test_run"]
    store.finish_run(run_id, "done", test_status=run.status, comment_id=ctx.comment_id)
    return run.status


def _give_up_or_retry(
    job: PRJob, ctx: graph.FlowContext, run_id: int, final: bool, error: Exception
) -> None:
    """환경 오류를 기록한다. 마지막 시도면 작업 키를 지워 동기화가 나중에 다시 넣을 수 있게 한다."""
    _report_environment(job, ctx, final, error)
    ctx.store.finish_run(run_id, "env_error", comment_id=ctx.comment_id)
    if final:
        ctx.store.release(job)  # 지우지 않으면 키 만료(6시간)까지 같은 커밋을 다시 넣지 못한다


def _report_environment(job: PRJob, ctx: graph.FlowContext, final: bool, error: Exception) -> None:
    log.warning("환경 오류 %s#%s: %s", job.repo, job.pr_number, error)
    try:
        ctx.comment_id = ctx.github.upsert_comment(
            job.repo, job.pr_number, report.render_environment_wait(job.head_sha, final=final),
            ctx.comment_id,
        )  # fmt: skip
    except GitHubError:
        log.warning("환경 오류 표시를 PR에 남기지 못함")  # gh 자체가 원인일 수 있다


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = config.load_config()
    redis = Redis.from_url(settings.redis_url)
    problems = preflight.failures(preflight.run_checks(redis))
    if problems:
        for problem in problems:
            log.error(problem)
        return 1
    # 동시 작업 1개. macOS에서 fork 문제를 피하려고 작업을 같은 프로세스에서 실행한다
    worker = SimpleWorker([Queue(state_keys.QUEUE_NAME, connection=redis)], connection=redis)
    worker.work(with_scheduler=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
