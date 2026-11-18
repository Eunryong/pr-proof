"""워커의 작업 흐름: LangGraph 직선 그래프 (체크아웃 → 규칙 검사 → 테스트 → 코멘트).

각 단계 사이에서 이 작업의 커밋이 아직 PR의 최신 커밋인지 확인하고, 낡았으면 멈춘다.
그래프 상태에는 데이터만 넣고, worktree 같은 자원은 FlowContext가 들고 정리한다.
체크포인터는 2단계(/fix 승인 대기)에서 넣는다.
"""

import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from app.models.finding import SkillResult
from app.models.job import PRJob
from app.models.test_run import PytestRun
from app.sandbox import container, docker_cli, pytest_runner, venv
from app.worker import base_config, repo, report
from app.worker.base_config import BaseConfig
from app.worker.github import GitHub
from app.worker.skills import lint
from app.worker.state import Store

Route = Literal["next", "stop"]


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
    올라간다. 처리는 worker/main.py가 정한다.
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


def build_graph(ctx: FlowContext):
    def checkout(state: FlowState) -> FlowState:
        job = state["job"]
        co = ctx.stack.enter_context(
            repo.checkout(ctx.data_dir, job.repo, job.pr_number, job.head_sha, job.base_sha)
        )
        ctx.worktree = co.path
        ctx.base = base_config.load(co.path, job.base_sha, ctx.max_memory_gb)
        return {}

    def progress(job: PRJob, step: str) -> None:
        ctx.comment_id = ctx.github.upsert_comment(
            job.repo, job.pr_number, report.render_progress(job.head_sha, step), ctx.comment_id
        )

    def is_stale_for(job: PRJob) -> Callable[[], bool]:
        def is_stale() -> bool:
            ctx.store.heartbeat(ctx.run_id)  # 10초마다 불리므로 생존 확인도 같이 남긴다
            return ctx.store.is_stale(job)

        return is_stale

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
        run = (ctx.run_tests or run_pr_tests)(
            ctx.worktree,
            job_id=f"{ctx.run_id}",
            is_stale=is_stale_for(job),
            venv_cache=ctx.data_dir / "venvs",
            memory_gb=ctx.base.memory_gb,
        )
        return {"test_run": run, **({"stopped": "cancelled"} if run.cancelled else {})}

    def comment(state: FlowState) -> FlowState:
        job = state["job"]
        body = report.render_result(
            state["test_run"],
            job.head_sha,
            time.monotonic() - ctx.started,
            lint=state["lint"],
            base=ctx.base,
            from_sync=job.source == "sync",
        )
        ctx.comment_id = ctx.github.upsert_comment(job.repo, job.pr_number, body, ctx.comment_id)
        return {"commented": True}

    def route(state: FlowState) -> Route:
        if state.get("stopped") or ctx.store.is_stale(state["job"]):
            return "stop"
        return "next"

    graph = StateGraph(FlowState)
    graph.add_node("checkout", checkout)
    graph.add_node("lint", lint_step)
    graph.add_node("test", test)
    graph.add_node("comment", comment)
    graph.add_edge(START, "checkout")
    graph.add_conditional_edges("checkout", route, {"next": "lint", "stop": END})
    graph.add_conditional_edges("lint", route, {"next": "test", "stop": END})
    graph.add_conditional_edges("test", route, {"next": "comment", "stop": END})
    graph.add_edge("comment", END)
    return graph.compile()


def run_flow(job: PRJob, ctx: FlowContext) -> FlowState:
    """그래프를 실행하고 worktree를 정리한다. 코멘트까지 가지 못했으면 stopped가 들어 있다."""
    with ctx.stack:
        result = build_graph(ctx).invoke({"job": job})
    if not result.get("commented"):
        result.setdefault("stopped", "stale")
    return result
