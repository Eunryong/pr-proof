"""`update` 작업: PR의 한 커밋을 검사하고 코멘트에 결과를 남긴다."""

import logging
from typing import Any, Final

from app.models.access import Author, NeedsApproval
from app.models.install import install_failure_outcome
from app.models.job import PRJob
from app.sandbox import docker_cli
from app.worker import doc_rules, graph, guard, proposal, report, retry
from app.worker.deps import Deps
from app.worker.github import PullRefs
from app.worker.lang.python import DependencyInstallError

CLOSED_HEAD: Final = "closed"  # PR이 닫혔다는 Redis 최신 head 표시. 어떤 커밋 SHA와도 같지 않다

log = logging.getLogger(__name__)


def _admit(job: PRJob, deps: Deps, final: bool) -> PullRefs | str:
    """GitHub에서 PR의 지금 상태를 확인하고 가드레일을 지난다 (8.6). 검사할 작업이면 조회한 PR을,
    아니면 결과 이름을 돌려준다.

    닫힌 PR은 검사하지 않는다. 닫히기 전에 등록된 작업이 닫힌 뒤에 실행될 수 있다(워커가 죽어 RQ가
    나중에 다시 실행한 경우). 그대로 두면 닫힌 PR의 코멘트를 고치고 제안 PR을 연다. 최신 head를
    닫힘 표시로 되돌려, 이미 돌고 있는 작업도 멈추게 한다.

    신뢰되지 않은 작성자의 PR이고 지금 head가 승인되지 않았으면 승인 안내만 달고 끝낸다. 같은
    조회에서 읽은 작성자 값으로 정한다. 조회나 base 설정 읽기에 실패하면 환경 오류로 재시도한다
    (아직 runs 기록 전이므로 마지막 시도면 작업 키만 지운다).
    """
    try:
        refs = deps.github.pr_refs(job.repo, job.pr_number)
        if not refs.open:
            deps.store.set_latest_head(job.repo, job.pr_number, CLOSED_HEAD)
            return "closed"
        gate = guard.check(job, deps, refs)
        if isinstance(gate.decision, NeedsApproval):
            return guard.block(job, deps, refs)
    except retry.EnvironmentFailure:
        if final:
            deps.store.release(job)
        raise
    return refs


def _context(deps: Deps, run_id: int, comment_id: int | None) -> graph.FlowContext:
    return graph.FlowContext(
        store=deps.store,
        github=deps.github,
        data_dir=deps.settings.data_dir,
        run_id=run_id,
        comment_id=comment_id,
        max_memory_gb=deps.settings.sandbox.max_memory_gb,
        call_maxima=deps.settings.model.calls,
        rag_model=deps.settings.rag.embed_model,
        sandbox_names=docker_cli.Names(
            image=deps.settings.sandbox.image,
            container_prefix=deps.settings.sandbox.container_prefix,
        ),
    )


def pending_fix(job: PRJob, deps: Deps, bundle: str) -> graph.PendingFix | None:
    """이 커밋에 `/fix`로 재개할 묶음의 수정이 있으면 그 종류 (승인 대기, 실패, 다시 돌리기)."""
    return graph.pending_kind(job, _context(deps, run_id=0, comment_id=None), bundle)


def resume_fix(job: PRJob, deps: Deps, approval: dict[str, Any], author: Author) -> str:
    """/fix 승인: 멈춰 있던 그래프를 수정 단계부터 이어 간다. job은 PR의 지금 head의 update 작업.

    가드레일(8.6)을 지난 명령 작업에서만 부른다. author는 그때 GitHub에서 읽은 PR 작성자다.
    """
    store = deps.store
    _, final = retry.attempt()
    store.set_latest_head(job.repo, job.pr_number, job.head_sha)
    run_id = store.start_run(job, kind="fix", author=author)
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
    # 다시 돌린 수정 판단이 모두 skip이면 그 묶음의 수정이 돌지 않는다
    done = result.get(graph.STATE_KEYS[approval["skill"]][0])
    status = done.status if done is not None else "no_targets"
    store.finish_run(run_id, "done", test_status=status, comment_id=ctx.comment_id)
    return f"fix:{status}"


def process_update(job: PRJob, deps: Deps) -> str:
    _, final = retry.attempt()
    admitted = _admit(job, deps, final)
    if isinstance(admitted, str):
        return admitted  # closed, needs_approval: 할 일이 없다
    return run_admitted(job, deps, admitted)


def run_admitted(job: PRJob, deps: Deps, refs: PullRefs) -> str:
    """가드레일(8.6)을 지난 update 작업을 돌린다. refs는 같은 작업에서 방금 조회한 PR이다.

    웹훅은 순서대로 오지 않을 수 있다. 옛 커밋의 웹훅이 늦게 오면 Redis의 최신 head가 옛 커밋으로
    덮이고, 그대로 두면 옛 커밋의 결과가 PR에 남는다. 그래서 최신 head를 GitHub의 값으로 바로잡고,
    이 작업의 커밋이 지금 head가 아니면 더 새 커밋의 작업(또는 동기화)에 맡긴다.
    """
    store, github = deps.store, deps.github
    attempt, final = retry.attempt()
    store.set_latest_head(job.repo, job.pr_number, refs.head_sha)
    if refs.head_sha != job.head_sha:
        return "stale"
    run_id = store.start_run(job, author=guard.author(refs))
    comment_id = store.last_comment_id(job.repo, job.pr_number)
    ctx = _context(deps, run_id, comment_id)
    try:
        ctx.comment_id = github.upsert_comment(
            job.repo, job.pr_number, report.render_progress(job.head_sha, "체크아웃"), comment_id
        )
        # 이전 커밋의 열린 제안을 정리한다. 머지됐는지도 여기서 알게 된다 (루프 방지에 쓴다)
        proposal.refresh(store, github, job.repo, job.pr_number)
        proposal.supersede(store, github, job.repo, job.pr_number, job.head_sha)
        result = graph.run_flow(job, ctx, retry=attempt > 1)
    except DependencyInstallError as e:
        if install_failure_outcome(e.kind, attempt) == "unrunnable":
            body = report.render_install_failure(
                job.head_sha, e.detail, base=ctx.base,
                doc_rules=ctx.doc_rules,
                checkers=doc_rules.shown_checkers(ctx.doc_rules, store), kind=e.kind,
                packages=e.packages,
            )  # fmt: skip
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
