"""재시도와 실패 기록 (docs/DESIGN.md F3·F8).

환경 오류는 `env_error`, 그 밖의 예외는 `error`로 runs에 남기고, 예외는 호출한 쪽이 다시 올려
RQ가 정해진 간격으로 재시도하게 한다. 마지막 시도면 작업 키를 지워 동기화가 판단하게 한다.
"""

import logging

from rq import get_current_job

from app.shared.job_pure import PRJob
from app.worker import graph, report
from app.worker.ai.model_backend import AIEnvironmentError
from app.worker.ai.workdir import WorkdirError
from app.worker.github import GitHubError
from app.worker.repo import GitError
from app.worker.sandbox.docker_cli import SandboxError

log = logging.getLogger(__name__)

# WorkdirError: 모델 전용 폴더를 만들거나 diff를 뽑는 호스트의 git이 실패했다. PR 탓이 아니다
EnvironmentFailure = (SandboxError, GitError, GitHubError, AIEnvironmentError, WorkdirError)


def attempt() -> tuple[int, bool]:
    """(이번 시도 번호, 마지막 시도인지). RQ 밖에서 부르면 (1, True)."""
    current = get_current_job()
    if current is None or current.retries_left is None:
        return 1, True
    total = len(current.retry_intervals or []) or 0
    number = total - current.retries_left + 1
    return number, current.retries_left == 0


def environment_failure(
    job: PRJob, ctx: graph.FlowContext, run_id: int, final: bool, error: Exception
) -> None:
    """환경 오류를 기록한다. 마지막 시도면 작업 키를 지워 동기화가 나중에 다시 넣을 수 있게 한다."""
    log.warning("환경 오류 %s#%s: %s", job.repo, job.pr_number, error)
    _show(job, ctx, report.render_environment_wait(job.head_sha, final=final))
    ctx.store.finish_run(run_id, "env_error", comment_id=ctx.comment_id)
    if final:
        ctx.store.release(job)  # 지우지 않으면 키 만료(6시간)까지 같은 커밋을 다시 넣지 못한다


def unexpected_error(
    job: PRJob, ctx: graph.FlowContext, run_id: int, final: bool, error: Exception
) -> None:
    """환경 오류가 아닌 예외(파이프라인 버그, rq JobTimeoutException)를 기록한다.

    runs가 running으로 남으면 동기화가 죽은 워커로 보고 같은 커밋을 계속 다시 넣는다.
    마지막 시도면 작업 키를 지운다 (동기화는 error를 같은 커밋으로 다시 넣지 않는다).
    """
    log.exception("작업 오류 %s#%s: %s", job.repo, job.pr_number, error)
    _show(job, ctx, report.render_pipeline_error(job.head_sha, final=final))
    ctx.store.finish_run(run_id, "error", comment_id=ctx.comment_id)
    if final:
        ctx.store.release(job)


def _show(job: PRJob, ctx: graph.FlowContext, body: str) -> None:
    try:
        ctx.comment_id = ctx.github.upsert_comment(job.repo, job.pr_number, body, ctx.comment_id)
    except GitHubError:
        log.warning("오류 표시를 PR에 남기지 못함")  # gh 자체가 원인일 수 있다
