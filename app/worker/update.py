"""`update` 작업: PR의 한 커밋을 검사하고 코멘트에 결과를 남긴다."""

from typing import Any, Final

from app.models.install import install_failure_outcome
from app.models.job import PRJob
from app.sandbox import docker_cli
from app.sandbox.venv import DependencyInstallError
from app.worker import graph, proposal, report, retry
from app.worker.deps import Deps
from app.worker.github import GitHubError

CLOSED_HEAD: Final = "closed"  # PR이 닫혔다는 Redis 최신 head 표시. 어떤 커밋 SHA와도 같지 않다


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


def _context(deps: Deps, run_id: int, comment_id: int | None) -> graph.FlowContext:
    return graph.FlowContext(
        store=deps.store,
        github=deps.github,
        data_dir=deps.settings.data_dir,
        run_id=run_id,
        comment_id=comment_id,
        max_memory_gb=deps.settings.sandbox.max_memory_gb,
        sandbox_names=docker_cli.Names(
            image=deps.settings.sandbox.image,
            container_prefix=deps.settings.sandbox.container_prefix,
        ),
    )


def has_pending_fix(job: PRJob, deps: Deps, skill: str = "lint") -> bool:
    """이 커밋에 `/fix <skill>`로 재개할 수정이 있는지 (승인 대기, 또는 수정 도중 실패)."""
    return graph.has_pending_fix(job, _context(deps, run_id=0, comment_id=None), skill)


def resume_fix(job: PRJob, deps: Deps, approval: dict[str, Any]) -> str:
    """/fix 승인: 멈춰 있던 그래프를 수정 단계부터 이어 간다. job은 PR의 지금 head의 update 작업."""
    store = deps.store
    _, final = retry.attempt()
    store.set_latest_head(job.repo, job.pr_number, job.head_sha)
    run_id = store.start_run(job, kind="fix")
    ctx = _context(deps, run_id, store.last_comment_id(job.repo, job.pr_number))
    try:
        result = graph.resume_fix(job, ctx, approval)
    # 수정 뒤 테스트의 설치 실패는 수정 전에 설치가 됐으므로 환경 쪽으로 본다
    except (DependencyInstallError, *retry.EnvironmentFailure) as e:
        retry.environment_failure(job, ctx, run_id, final, e)
        raise
    except Exception as e:  # 파이프라인 버그, rq JobTimeoutException
        retry.unexpected_error(job, ctx, run_id, final, e)
        raise
    if result is None:
        store.finish_run(run_id, "done", comment_id=ctx.comment_id)
        return "no_pending_fix"
    if result.get("stopped"):
        store.finish_run(run_id, "cancelled", comment_id=ctx.comment_id)
        return "stale"
    outcome = result["fixes"][approval["skill"]]
    store.finish_run(run_id, "done", test_status=outcome.status, comment_id=ctx.comment_id)
    return f"fix:{outcome.status}"


def process_update(job: PRJob, deps: Deps) -> str:
    store, github = deps.store, deps.github
    attempt, final = retry.attempt()
    if not _is_current_head(job, deps, final):
        return "stale"  # 더 새 커밋이 있다. 그 커밋의 작업(또는 동기화)이 처리한다
    run_id = store.start_run(job)
    comment_id = store.last_comment_id(job.repo, job.pr_number)
    ctx = _context(deps, run_id, comment_id)
    try:
        ctx.comment_id = github.upsert_comment(
            job.repo, job.pr_number, report.render_progress(job.head_sha, "체크아웃"), comment_id
        )
        # 이전 커밋의 열린 제안을 정리한다. 머지됐는지도 여기서 알게 된다 (루프 방지에 쓴다)
        proposal.refresh(store, github, job.repo, job.pr_number)
        proposal.supersede(store, github, job.repo, job.pr_number, job.head_sha)
        result = graph.run_flow(job, ctx)
    except DependencyInstallError as e:
        if install_failure_outcome(e.kind, attempt) == "unrunnable":
            body = report.render_install_failure(
                job.head_sha, e.detail, lint=ctx.lint, static=ctx.static, base=ctx.base
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


def process_close(job: PRJob, deps: Deps) -> str:
    """원본 PR이 닫혔다: 진행 중인 검사를 멈추고, 남은 제안 PR을 닫고 브랜치를 지운다 (F10).

    최신 head를 닫힘 표시로 바꾸면 실행 중인 작업이 10초 안에 낡은 작업으로 보고 멈춘다.
    다시 열리면(reopened) 웹훅이 최신 head를 새로 적는다.

    먼저 GitHub에서 PR이 정말 닫혔는지 본다. closed 웹훅이 reopened보다 늦게 오거나, 동기화의
    열린 PR 목록이 잘려 열린 PR이 빠지면, 열린 PR의 제안을 닫게 된다.
    """
    _, final = retry.attempt()
    try:
        if deps.github.pr_refs(job.repo, job.pr_number).open:
            return "still_open"
        deps.store.set_latest_head(job.repo, job.pr_number, CLOSED_HEAD)
        closed = proposal.close_all(deps.store, deps.github, job.repo, job.pr_number)
    except Exception:
        if final:
            deps.store.release(job)
        raise
    return f"closed:{closed}"
