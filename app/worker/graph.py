"""워커의 작업 흐름 (LangGraph).

    체크아웃 → 규칙 검사 → 테스트 → 코멘트 ─┬─ (제안할 수정 없음) → 끝
                                     ├─ 승인 대기(interrupt) ─ /fix lint → 수정 → 수정 코멘트
                                     └─ (require_approval: false) ───────→ 수정 → 수정 코멘트

각 단계 사이에서 이 작업의 커밋이 아직 PR의 최신 커밋인지 확인하고, 낡았으면 멈춘다.
그래프 상태에는 데이터만 넣고, worktree 같은 자원은 FlowContext가 들고 정리한다.
상태는 SQLite 체크포인트에 남고 thread_id는 `repo#pr#head_sha`다. 새 커밋이 오면 SHA가 달라져
멈춰 있던 그래프는 따로 지우지 않아도 쓰이지 않는다. 재개할 때 worktree는 다시 만든다 (F7).
"""

import sqlite3
import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, TypedDict

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.models import fix as fix_rules
from app.models.finding import SkillResult
from app.models.fix import FixOutcome
from app.models.job import PRJob
from app.models.test_run import PytestRun
from app.sandbox import container, docker_cli, pytest_runner, venv
from app.worker import base_config, repo, report
from app.worker.base_config import BaseConfig
from app.worker.github import GitHub
from app.worker.skills import lint
from app.worker.skills.lint import LintFix
from app.worker.state import Store

Route = Literal["next", "stop"]
CHECKPOINT_DB: Final = "checkpoints.db"
FIX_DIR: Final = "fixes"
AWAIT_FIX: Final = "await_fix"
FIX: Final = "fix"
# 체크포인트에서 되살릴 수 있는 상태 타입 (나머지는 langgraph가 막는다)
_STATE_TYPES: Final = (
    ("app.models.job", "PRJob"),
    ("app.models.finding", "Finding"),
    ("app.models.finding", "SkillResult"),
    ("app.models.test_run", "PytestRun"),
    ("app.models.test_run", "ReportSummary"),
    ("app.models.fix", "FixOutcome"),
)


def run_pr_tests(
    worktree: Path,
    *,
    job_id: str | None = None,
    is_stale: Callable[[], bool] | None = None,
    venv_cache: Path = venv.VENV_CACHE,
    memory_gb: float = docker_cli.MEMORY_LIMIT_GB,
) -> PytestRun:
    """테스트 단계: 이미지 준비 → .venv 준비 → 작업 컨테이너에서 pytest → 결과.

    Docker 실패는 docker_cli.SandboxError(환경 오류), pip 실패는 venv.DependencyInstallError로
    올라간다. 처리는 worker/update.py가 정한다.
    """
    image = docker_cli.ensure_image()
    venv_dir = venv.ensure_venv(worktree, image, cache_dir=venv_cache, memory_gb=memory_gb)
    with container.running(worktree, venv_dir, job_id=job_id, memory_gb=memory_gb) as name:
        return pytest_runner.run_tests(name, worktree, is_stale=is_stale)


class FlowState(TypedDict, total=False):
    job: PRJob
    lint: SkillResult
    test_run: PytestRun
    commented: bool
    fix: FixOutcome
    stopped: Literal["stale", "cancelled"]


@dataclass
class FlowContext:
    store: Store
    github: GitHub
    data_dir: Path
    run_id: int
    comment_id: int | None
    max_memory_gb: float
    started: float = field(default_factory=time.monotonic)
    stack: ExitStack = field(default_factory=ExitStack)
    worktree: Path | None = None
    base: BaseConfig | None = None
    # 의존성 설치가 실패해도(그래프 밖 예외) 코멘트에 규칙 결과를 쓰려고 여기에도 둔다
    lint: SkillResult | None = None
    run_tests: Callable[..., PytestRun] | None = None  # 없으면 run_pr_tests
    run_lint: Callable[..., SkillResult | None] | None = None  # 없으면 lint.run_lint
    fix_lint: Callable[..., LintFix | None] | None = None  # 없으면 lint.fix_lint


def thread_id(repo_name: str, pr_number: int, head_sha: str) -> str:
    return f"{repo_name}#{pr_number}#{head_sha}"


def thread_config(job: PRJob) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id(job.repo, job.pr_number, job.head_sha)}}


def open_checkpointer(data_dir: Path, stack: ExitStack) -> SqliteSaver:
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(data_dir / CHECKPOINT_DB, check_same_thread=False)
    stack.callback(conn.close)
    return SqliteSaver(conn, serde=JsonPlusSerializer(allowed_msgpack_modules=list(_STATE_TYPES)))


def build_graph(ctx: FlowContext, checkpointer: SqliteSaver | None = None):
    def enter_checkout(job: PRJob) -> None:
        co = ctx.stack.enter_context(
            repo.checkout(ctx.data_dir, job.repo, job.pr_number, job.head_sha, job.base_sha)
        )
        ctx.worktree = co.path
        ctx.base = base_config.load(co.path, job.base_sha, ctx.max_memory_gb)

    def progress(job: PRJob, step: str) -> None:
        ctx.comment_id = ctx.github.upsert_comment(
            job.repo, job.pr_number, report.render_progress(job.head_sha, step), ctx.comment_id
        )

    def is_stale_for(job: PRJob) -> Callable[[], bool]:
        def is_stale() -> bool:
            ctx.store.heartbeat(ctx.run_id)  # 10초마다 불리므로 생존 확인도 같이 남긴다
            return ctx.store.is_stale(job)

        return is_stale

    def tests_for(job: PRJob) -> PytestRun:
        return (ctx.run_tests or run_pr_tests)(
            ctx.worktree,
            job_id=f"{ctx.run_id}",
            is_stale=is_stale_for(job),
            venv_cache=ctx.data_dir / "venvs",
            memory_gb=ctx.base.memory_gb,
        )

    def checkout(state: FlowState) -> FlowState:
        enter_checkout(state["job"])
        return {}

    def lint_step(state: FlowState) -> FlowState:
        job = state["job"]
        progress(job, "규칙 검사")
        result = (ctx.run_lint or lint.run_lint)(
            ctx.worktree, ctx.base, job_id=f"{ctx.run_id}", is_stale=is_stale_for(job)
        )
        if result is None:
            return {"stopped": "cancelled"}
        ctx.lint = result
        return {"lint": result}

    def test(state: FlowState) -> FlowState:
        job = state["job"]
        progress(job, "테스트 실행")
        run = tests_for(job)
        return {"test_run": run, **({"stopped": "cancelled"} if run.cancelled else {})}

    def proposal(state: FlowState) -> report.Proposal:
        blocker = fix_rules.lint_fix_blocker(state.get("lint"), state["test_run"])
        if blocker is not None:
            return blocker
        return "approval" if ctx.base.repo.skills.fix.require_approval else "auto"

    def comment(state: FlowState) -> FlowState:
        job = state["job"]
        body = report.render_result(
            state["test_run"],
            job.head_sha,
            time.monotonic() - ctx.started,
            lint=state["lint"],
            base=ctx.base,
            proposal=proposal(state),
            from_sync=job.source == "sync",
        )
        ctx.comment_id = ctx.github.upsert_comment(job.repo, job.pr_number, body, ctx.comment_id)
        return {"commented": True}

    def after_comment(state: FlowState) -> str:
        if ctx.store.is_stale(state["job"]):
            return END
        match proposal(state):
            case "approval":
                return AWAIT_FIX
            case "auto":
                return FIX
            case _:
                return END

    def await_fix(state: FlowState) -> FlowState:
        # 상태를 체크포인트에 남기고 멈춘다. 워커는 다른 작업을 처리한다.
        # /fix lint가 오면 이 node부터 재개된다
        interrupt({"skill": "lint", "head_sha": state["job"].head_sha})
        return {}

    def fix(state: FlowState) -> FlowState:
        job = state["job"]
        if ctx.worktree is None:  # 재개: 멈춘 사이 worktree는 지워졌다
            enter_checkout(job)
        progress(job, "lint 수정")
        result = (ctx.fix_lint or lint.fix_lint)(
            ctx.worktree, ctx.base, job.base_sha, job_id=f"{ctx.run_id}",
            is_stale=is_stale_for(job),
        )  # fmt: skip
        if result is None:
            return {"stopped": "cancelled"}
        outcome = verify_fix(job, state, result)
        if outcome is None:
            return {"stopped": "cancelled"}
        return {"fix": outcome}

    def verify_fix(job: PRJob, state: FlowState, result: LintFix) -> FixOutcome | None:
        """수정안을 결정적으로 검증한다. 새 커밋이 와서 중단되면 None."""
        before = state["lint"].findings
        if result.applied == 0:
            return FixOutcome(skill="lint", status="nothing_fixed", remaining=tuple(before))
        if not result.after.checked:
            return FixOutcome(skill="lint", status="unchecked", detail=result.after.error or "")
        fixed, new = fix_rules.compare(before, result.after)
        remaining = tuple(result.after.findings)
        if new:
            detail = ", ".join(f"{f.file}:{f.line} {f.code}" for f in new)
            return FixOutcome(
                skill="lint", status="new_violations", remaining=remaining, detail=detail
            )
        bad = fix_rules.out_of_scope(
            fix_rules.parse_porcelain(repo.status(ctx.worktree)), result.touched_files
        )
        if bad:
            return FixOutcome(skill="lint", status="out_of_scope", detail=", ".join(bad))
        progress(job, "lint 수정 검증 (테스트)")
        after_run = tests_for(job)
        if after_run.cancelled:
            return None
        if not fix_rules.same_test_result(state["test_run"], after_run):
            return FixOutcome(
                skill="lint", status="tests_changed", test_run=after_run,
                detail=report.format_test_line(after_run, ctx.base.memory_gb),
            )  # fmt: skip
        return FixOutcome(
            skill="lint",
            status="ready",
            fixed=fixed,
            remaining=remaining,
            test_run=after_run,
            changed_files=result.touched_files,
            patch_path=save_patch(job, repo.head_diff(ctx.worktree)),
        )

    def save_patch(job: PRJob, patch: str) -> Path:
        out = ctx.data_dir / FIX_DIR / f"{job.repo.replace('/', '__')}-pr{job.pr_number}"
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{job.head_sha[:12]}-lint.patch"
        path.write_text(patch)
        return path

    def fix_comment(state: FlowState) -> FlowState:
        job = state["job"]
        body = report.render_result(
            state["test_run"],
            job.head_sha,
            time.monotonic() - ctx.started,
            lint=state["lint"],
            base=ctx.base,
            fix=state["fix"],
            from_sync=job.source == "sync",
        )
        ctx.comment_id = ctx.github.upsert_comment(job.repo, job.pr_number, body, ctx.comment_id)
        return {}

    def route(state: FlowState) -> Route:
        if state.get("stopped") or ctx.store.is_stale(state["job"]):
            return "stop"
        return "next"

    graph = StateGraph(FlowState)
    graph.add_node("checkout", checkout)
    graph.add_node("lint", lint_step)
    graph.add_node("test", test)
    graph.add_node("comment", comment)
    graph.add_node(AWAIT_FIX, await_fix)
    graph.add_node(FIX, fix)
    graph.add_node("fix_comment", fix_comment)
    graph.add_edge(START, "checkout")
    graph.add_conditional_edges("checkout", route, {"next": "lint", "stop": END})
    graph.add_conditional_edges("lint", route, {"next": "test", "stop": END})
    graph.add_conditional_edges("test", route, {"next": "comment", "stop": END})
    graph.add_conditional_edges("comment", after_comment, [AWAIT_FIX, FIX, END])
    graph.add_edge(AWAIT_FIX, FIX)
    graph.add_conditional_edges(FIX, route, {"next": "fix_comment", "stop": END})
    graph.add_edge("fix_comment", END)
    return graph.compile(checkpointer=checkpointer)


def run_flow(job: PRJob, ctx: FlowContext) -> FlowState:
    """새 검사를 처음부터 실행한다. 같은 커밋의 이전 체크포인트는 지운다 (/recheck 등).

    코멘트까지 가지 못했으면 stopped가 들어 있다. 승인 대기로 멈추면 commented만 있다.
    """
    with ctx.stack:
        saver = open_checkpointer(ctx.data_dir, ctx.stack)
        config = thread_config(job)
        saver.delete_thread(config["configurable"]["thread_id"])
        result = build_graph(ctx, saver).invoke({"job": job}, config)
    if not result.get("commented"):
        result.setdefault("stopped", "stale")
    return result


PendingFix = Literal["awaiting", "interrupted_fix"]


def pending_fix(job: PRJob, ctx: FlowContext, saver: SqliteSaver) -> PendingFix | None:
    """이 커밋에 재개할 수정이 있는지. 승인 대기 중이거나, 수정 도중 실패해 다시 시도할 수 있다."""
    snapshot = build_graph(ctx, saver).get_state(thread_config(job))
    if AWAIT_FIX in snapshot.next and snapshot.interrupts:
        return "awaiting"
    if FIX in snapshot.next:
        return "interrupted_fix"
    return None


def has_pending_fix(job: PRJob, ctx: FlowContext) -> bool:
    with ExitStack() as stack:
        return pending_fix(job, ctx, open_checkpointer(ctx.data_dir, stack)) is not None


def resume_fix(job: PRJob, ctx: FlowContext, approval: dict[str, Any]) -> FlowState | None:
    """멈춰 있던 그래프를 수정 단계부터 이어서 실행한다. 재개할 것이 없으면 None."""
    with ctx.stack:
        saver = open_checkpointer(ctx.data_dir, ctx.stack)
        pending = pending_fix(job, ctx, saver)
        if pending is None:
            return None
        graph = build_graph(ctx, saver)
        command = Command(resume=approval) if pending == "awaiting" else None
        result = graph.invoke(command, thread_config(job))
    if "fix" not in result:
        result.setdefault("stopped", "stale")
    return result
