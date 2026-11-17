"""`update` 작업: PR의 한 커밋을 검사하고 코멘트에 결과를 남긴다."""

from app.models.install import install_failure_outcome
from app.models.job import PRJob
from app.sandbox.venv import DependencyInstallError
from app.worker import graph, report, retry
from app.worker.deps import Deps
from app.worker.github import GitHubError


def _is_current_head(job: PRJob, deps: Deps, final: bool) -> bool:
    """GitHub에서 PR의 지금 head를 확인하고 Redis의 최신 head를 그 값으로 바로잡는다.

    웹훅은 순서대로 오지 않을 수 있다. 옛 커밋의 웹훅이 늦게 오면 Redis의 최신 head가 옛 커밋으로
    덮이고, 그대로 두면 옛 커밋의 결과가 PR에 남는다. 확인에 실패하면 환경 오류로 재시도한다
    (아직 runs 기록 전이므로 마지막 시도면 작업 키만 지운다).
    """
    try:
        actual = deps.github.pr_refs(job.repo, job.pr_number).head_sha
    except GitHubError:
        if final:
            deps.store.release(job)
        raise
    deps.store.set_latest_head(job.repo, job.pr_number, actual)
    return actual == job.head_sha


def process_update(job: PRJob, deps: Deps) -> str:
    store, github = deps.store, deps.github
    attempt, final = retry.attempt()
    if not _is_current_head(job, deps, final):
        return "stale"  # 더 새 커밋이 있다. 그 커밋의 작업(또는 동기화)이 처리한다
    run_id = store.start_run(job)
    comment_id = store.last_comment_id(job.repo, job.pr_number)
    ctx = graph.FlowContext(
        store=store,
        github=github,
        data_dir=deps.settings.data_dir,
        run_id=run_id,
        comment_id=comment_id,
        max_memory_gb=deps.settings.sandbox.max_memory_gb,
    )
    try:
        ctx.comment_id = github.upsert_comment(
            job.repo, job.pr_number, report.render_progress(job.head_sha, "체크아웃"), comment_id
        )
        result = graph.run_flow(job, ctx)
    except DependencyInstallError as e:
        if install_failure_outcome(e.kind, attempt) == "unrunnable":
            body = report.render_install_failure(
                job.head_sha, e.detail, lint=ctx.lint, base=ctx.base
            )
            ctx.comment_id = github.upsert_comment(job.repo, job.pr_number, body, ctx.comment_id)
            store.finish_run(run_id, "done", test_status="unrunnable", comment_id=ctx.comment_id)
            return "unrunnable"
        retry.environment_failure(job, ctx, run_id, final, e)
        raise
    except retry.EnvironmentFailure as e:
        retry.environment_failure(job, ctx, run_id, final, e)
        raise
    except Exception as e:  # 파이프라인 버그, rq JobTimeoutException(작업 시간 초과)
        retry.unexpected_error(job, ctx, run_id, final, e)
        raise

    if result.get("stopped"):
        store.finish_run(run_id, "cancelled", comment_id=ctx.comment_id)
        return "stale"  # 새 커밋의 작업이 코멘트를 이어서 고친다
    run = result["test_run"]
    store.finish_run(run_id, "done", test_status=run.status, comment_id=ctx.comment_id)
    return run.status
