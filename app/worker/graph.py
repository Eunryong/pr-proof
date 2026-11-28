"""워커의 작업 흐름 (LangGraph, docs/DESIGN.md 5.1).

    체크아웃 → 규칙 읽기 → 검사 방법 정하기 → 위반 찾기 → 모델 판단 → PR 단위 확인 → PR 설명 확인
        → 이슈 확인 → 테스트 → 수정 판단 → 코멘트
        ─┬─ (고칠 위반 없음) → 끝
         ├─ (기본: 바로) 기계적 수정 → 수정안 PR ─┬→ 수정 코멘트 ─┬─ (남은 묶음 없음) → 끝
         ├─ (기본: 바로) 모델 수정 → 수정안 PR ───┘              └─ 다른 묶음의 대기·수정
         └─ 승인 대기(interrupt, `require_approval: true`) ─ /fix <묶음>

상태·컨텍스트·체크포인트 정의는 `app/worker/flow_state.py`, 각 단계의 node와 분기 함수는
`app/worker/flow_nodes.py`에 있다. 이 모듈은 테스트 실행, 그래프 조립, 체크포인트 기반 실행·재개
진입점을 맡고, 바깥에서 import하던 기호를 그대로 내보낸다.
"""

from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.shared.job_pure import PRJob
from app.worker import flow_nodes, records
from app.worker.fix import fix_decision_pure, proposal
from app.worker.flow_state import (
    _STATE_TYPES,
    AUTHOR_CHECKERS,
    AWAIT_FIX,
    BUNDLES,
    CHECKPOINT_DB,
    DECIDE,
    DELIVER_MECHANICAL,
    DELIVER_MODEL,
    DELIVER_NODES,
    DETECT,
    FIX_COMMENT,
    FIX_MECHANICAL,
    FIX_MODEL,
    FIX_NODES,
    JUDGE,
    LINKED_ISSUES,
    LOAD_RULES,
    MECHANICAL,
    MODEL,
    PR_CHECK,
    PR_DESCRIPTION,
    RESUMABLE,
    STATE_KEYS,
    TEST,
    TOOL_DIR,
    FlowContext,
    FlowState,
    PendingFix,
    Route,
    enter_checkout,
    is_stale_for,
    open_checkpointer,
    thread_config,
    thread_id,
    timed,
)
from app.worker.git import repo
from app.worker.lang import python
from app.worker.rules import doc_rules, pr_check_pure
from app.worker.rules.doc_rules_pure import Found
from app.worker.sandbox import container, docker_cli, toolcache
from app.worker.testing.generated_tests_pure import Suite
from app.worker.testing.test_run_pure import NO_PROFILE_WHY, TestRun

__all__ = [
    "AUTHOR_CHECKERS",
    "AWAIT_FIX",
    "BUNDLES",
    "CHECKPOINT_DB",
    "DECIDE",
    "DELIVER_MECHANICAL",
    "DELIVER_MODEL",
    "DELIVER_NODES",
    "DETECT",
    "FIX_COMMENT",
    "FIX_MECHANICAL",
    "FIX_MODEL",
    "FIX_NODES",
    "JUDGE",
    "LINKED_ISSUES",
    "LOAD_RULES",
    "MECHANICAL",
    "MODEL",
    "PR_CHECK",
    "PR_DESCRIPTION",
    "RESUMABLE",
    "STATE_KEYS",
    "TEST",
    "TOOL_DIR",
    "FlowContext",
    "FlowState",
    "PendingFix",
    "Route",
    "_STATE_TYPES",
    "build_graph",
    "carry_flow",
    "checked_values",
    "finished_values",
    "open_checkpointer",
    "pending_fix",
    "pending_kind",
    "pr_suite",
    "pr_tests",
    "resume_fix",
    "run_flow",
    "run_pr_tests",
    "thread_config",
    "thread_id",
    "timed",
]


def run_pr_tests(
    worktree: Path,
    *,
    job_id: str | None = None,
    is_stale: Callable[[], bool] | None = None,
    venv_cache: Path = python.VENV_CACHE,
    tool_cache: Path = toolcache.TOOL_CACHE,
    memory_gb: float = docker_cli.MEMORY_LIMIT_GB,
    names: docker_cli.Names = docker_cli.DEFAULT_NAMES,
    contexts: bool = False,
) -> TestRun:
    """테스트 단계: 이미지 준비 → Python 런타임(도구 캐시) → .venv 준비 → 작업 컨테이너에서 pytest
    → 결과. 저장소에 Python 프로필이 돌릴 것이 없으면 아무것도 준비하지 않고 `no_env`다.

    Docker 실패와 런타임을 받지 못함은 docker_cli.SandboxError(환경 오류), pip 실패는
    python.DependencyInstallError로 올라간다. 처리는 app/worker/update.py가 정한다.
    """
    if not python.has_work(worktree):
        return TestRun(unplanned=NO_PROFILE_WHY)
    image = docker_cli.ensure_image(names.image)
    runtime = python.ensure_runtime(tool_cache, names.image)
    venv_dir = python.ensure_venv(
        worktree, image, runtime, cache_dir=venv_cache, tag=names.image, memory_gb=memory_gb
    )
    mounts = [(venv_dir, python.VENV_IN_CONTAINER), (runtime.dir, python.RUNTIME_IN_CONTAINER)]
    with container.running(
        worktree, mounts, job_id=job_id, memory_gb=memory_gb, names=names
    ) as name:
        return python.run_tests(name, worktree, is_stale=is_stale, contexts=contexts)


def pr_tests(
    ctx: FlowContext, job: PRJob, worktree: Path | None = None, *, contexts: bool = False
) -> TestRun:
    """ctx의 worktree(또는 worktree)에서 PR의 테스트를 샌드박스로 돌린다. 테스트 node와 수정 단계,
    코멘트 수정(4.11)이 같이 쓴다."""
    kw = {"contexts": True} if contexts else {}
    return (ctx.run_tests or run_pr_tests)(
        worktree or ctx.worktree,
        job_id=f"{ctx.run_id}",
        is_stale=is_stale_for(ctx, job),
        venv_cache=ctx.data_dir / "venvs",
        tool_cache=ctx.data_dir / TOOL_DIR,
        memory_gb=ctx.base.memory_gb,
        names=ctx.sandbox_names,
        **kw,
    )


def pr_suite(
    ctx: FlowContext, job: PRJob, generated: Mapping[str, bytes] | None, files: Sequence[str]
) -> Suite:
    """수정 작성의 테스트 도구와 출구 검사의 테스트 (4.7, 4.8의 6번). generated가 있으면 PR
    head의 따로 된 worktree에 그 테스트 파일만 얹어 고치기 전 코드로 돌린다."""
    if generated is None:
        return python.read_suite(pr_tests(ctx, job, contexts=True), ctx.worktree, files)
    with repo.side_worktree(ctx.worktree, job.head_sha) as side:
        for rel, data in generated.items():
            (side / rel).parent.mkdir(parents=True, exist_ok=True)
            (side / rel).write_bytes(data)
        return python.read_suite(pr_tests(ctx, job, side, contexts=True), side, files)


def build_graph(ctx: FlowContext, checkpointer: SqliteSaver | None = None):
    tests_for = lambda job: pr_tests(ctx, job)  # noqa: E731
    suite_for = lambda job, generated, files: pr_suite(ctx, job, generated, files)  # noqa: E731
    route = lambda state: flow_nodes.route(ctx, state)  # noqa: E731
    after_doc = lambda state: flow_nodes.after_doc_step(ctx, state)  # noqa: E731
    next_fix = lambda state: flow_nodes.next_fix(ctx, state)  # noqa: E731

    fix_targets = [AWAIT_FIX, FIX_MECHANICAL, FIX_MODEL, END]
    graph = StateGraph(FlowState)
    graph.add_node("checkout", timed(ctx, "checkout", lambda s: flow_nodes.checkout(ctx, s)))
    graph.add_node(LOAD_RULES, timed(ctx, LOAD_RULES, lambda s: flow_nodes.load_rules(ctx, s)))
    graph.add_node(
        AUTHOR_CHECKERS,
        timed(ctx, AUTHOR_CHECKERS, lambda s: flow_nodes.author_checkers(ctx, s)),
    )
    graph.add_node(DETECT, timed(ctx, DETECT, lambda s: flow_nodes.detect_rules(ctx, s)))
    graph.add_node(JUDGE, timed(ctx, JUDGE, lambda s: flow_nodes.judge_rules(ctx, s)))
    graph.add_node(PR_CHECK, timed(ctx, PR_CHECK, lambda s: flow_nodes.pr_check_rules(ctx, s)))
    graph.add_node(
        PR_DESCRIPTION,
        timed(ctx, PR_DESCRIPTION, lambda s: flow_nodes.describe_pr(ctx, s)),
    )
    graph.add_node(
        LINKED_ISSUES,
        timed(ctx, LINKED_ISSUES, lambda s: flow_nodes.check_issues(ctx, s)),
    )
    graph.add_node(TEST, timed(ctx, TEST, lambda s: flow_nodes.test(ctx, s, tests_for=tests_for)))
    graph.add_node(DECIDE, timed(ctx, DECIDE, lambda s: flow_nodes.decide_fixes(ctx, s)))
    graph.add_node("comment", timed(ctx, "comment", lambda s: flow_nodes.comment(ctx, s)))
    graph.add_node(AWAIT_FIX, timed(ctx, AWAIT_FIX, lambda s: flow_nodes.await_fix(ctx, s)))
    graph.add_node(
        FIX_MECHANICAL,
        timed(
            ctx, FIX_MECHANICAL, lambda s: flow_nodes.fix_mechanical(ctx, s, tests_for=tests_for)
        ),
    )
    graph.add_node(
        DELIVER_MECHANICAL,
        timed(ctx, DELIVER_MECHANICAL, lambda s: flow_nodes.deliver_mechanical(ctx, s)),
    )
    graph.add_node(
        FIX_MODEL,
        timed(ctx, FIX_MODEL, lambda s: flow_nodes.fix_model(ctx, s, suite_for=suite_for)),
    )
    graph.add_node(
        DELIVER_MODEL,
        timed(ctx, DELIVER_MODEL, lambda s: flow_nodes.deliver_model(ctx, s)),
    )
    graph.add_node(FIX_COMMENT, timed(ctx, FIX_COMMENT, lambda s: flow_nodes.fix_comment(ctx, s)))
    graph.add_edge(START, "checkout")
    graph.add_conditional_edges("checkout", route, {"next": LOAD_RULES, "stop": END})
    graph.add_conditional_edges(
        LOAD_RULES, after_doc, {"next": AUTHOR_CHECKERS, "done": PR_DESCRIPTION, "stop": END}
    )
    graph.add_conditional_edges(
        AUTHOR_CHECKERS, after_doc, {"next": DETECT, "done": PR_DESCRIPTION, "stop": END}
    )
    graph.add_conditional_edges(DETECT, route, {"next": JUDGE, "stop": END})
    graph.add_conditional_edges(JUDGE, route, {"next": PR_CHECK, "stop": END})
    graph.add_conditional_edges(PR_CHECK, route, {"next": PR_DESCRIPTION, "stop": END})
    graph.add_conditional_edges(PR_DESCRIPTION, route, {"next": LINKED_ISSUES, "stop": END})
    graph.add_conditional_edges(LINKED_ISSUES, route, {"next": TEST, "stop": END})
    graph.add_conditional_edges(TEST, route, {"next": DECIDE, "stop": END})
    graph.add_conditional_edges(DECIDE, route, {"next": "comment", "stop": END})
    graph.add_conditional_edges("comment", next_fix, fix_targets)
    graph.add_conditional_edges(AWAIT_FIX, flow_nodes.after_await, [FIX_MECHANICAL, FIX_MODEL, END])
    graph.add_conditional_edges(FIX_MECHANICAL, route, {"next": DELIVER_MECHANICAL, "stop": END})
    graph.add_conditional_edges(DELIVER_MECHANICAL, route, {"next": FIX_COMMENT, "stop": END})
    graph.add_conditional_edges(FIX_MODEL, route, {"next": DELIVER_MODEL, "stop": END})
    graph.add_conditional_edges(DELIVER_MODEL, route, {"next": FIX_COMMENT, "stop": END})
    graph.add_conditional_edges(FIX_COMMENT, next_fix, fix_targets)
    return graph.compile(checkpointer=checkpointer)


def run_flow(job: PRJob, ctx: FlowContext, *, retry: bool = False) -> FlowState:
    """새 검사를 처음부터 실행한다. 같은 커밋의 이전 체크포인트는 지운다 (/recheck 등).

    retry는 같은 작업을 RQ가 다시 시도하는 것이다. 지난 시도가 전달(`deliver_*`)에서 실패해
    멈췄으면 처음부터 하지 않고 그 node부터 이어 간다 (5.6). 끝난 node는 다시 돌지 않으므로 모델
    호출 기록도 늘지 않는다.

    코멘트까지 가지 못했으면 stopped가 들어 있다. 승인 대기로 멈추면 commented만 있다.
    """
    with ctx.stack:
        saver = open_checkpointer(ctx.data_dir, ctx.stack)
        config = thread_config(job)
        flow = build_graph(ctx, saver)
        if retry and RESUMABLE & set(flow.get_state(config).next):
            result = flow.invoke(None, config)
        else:
            saver.delete_thread(config["configurable"]["thread_id"])
            result = flow.invoke({"job": job}, config)
    if not result.get("commented"):
        result.setdefault("stopped", "stale")
    return result


def checked_values(job: PRJob, ctx: FlowContext) -> Mapping[str, Any]:
    """이 커밋의 규칙 검사가 체크포인트에 남긴 상태(규칙 결과, 테스트 결과 등). 없으면 빈 dict.

    코멘트 수정(4.11)이 같은 커밋에서 이미 한 실행을 되풀이하지 않으려고 읽는다. 그래프를 돌리지
    않는다.
    """
    with ExitStack() as stack:
        saver = open_checkpointer(ctx.data_dir, stack)
        return dict(build_graph(ctx, saver).get_state(thread_config(job)).values)


def finished_values(job: PRJob, ctx: FlowContext) -> tuple[Mapping[str, Any], bool]:
    """이 커밋의 체크포인트 상태와, 그래프가 멈춘 node 없이 코멘트까지 갔는지. 그래프를 돌리지
    않는다. 수정안을 머지한 커밋이 검증한 커밋의 결과를 옮겨 적을 때 읽는다 (`carry_flow`)."""
    with ExitStack() as stack:
        snapshot = build_graph(ctx, open_checkpointer(ctx.data_dir, stack)).get_state(
            thread_config(job)
        )
    values = dict(snapshot.values)
    return values, not snapshot.next and bool(values.get("commented"))


def carry_flow(job: PRJob, ctx: FlowContext, values: FlowState) -> FlowState:
    """수정안을 머지한 커밋에 검증한 결과(values)를 옮겨 적는다. 규칙 검사, 테스트, 수정, 모델
    호출은 하지 않는다 (4.9 "수정안 PR 상태 관리").

    이 커밋의 체크포인트를 values로 채워 수정 판단이 끝난 것처럼 두고 코멘트 node만 돌린다. 그래서
    결과 코멘트는 보통의 검사와 같은 길로 그려지고, 이 커밋의 체크포인트(코멘트 수정이 읽는다)와
    위반 기록(다음 커밋의 "작성자가 고친 위반"이 읽는다)도 보통의 검사처럼 남는다. 체크아웃은 base
    설정과 위반 기록의 앞뒤 줄을 읽으려고 한다.
    """
    with ctx.stack:
        saver = open_checkpointer(ctx.data_dir, ctx.stack)
        config = thread_config(job)
        flow = build_graph(ctx, saver)
        saver.delete_thread(config["configurable"]["thread_id"])
        enter_checkout(ctx, job)
        found = values["doc_rules"]
        ctx.doc_rules = found
        if isinstance(found, Found):
            # 수정안이 고친 위반은 작성자가 아니라 수정안이 고친 것이다 (그 채택 기록은 수정안 쪽)
            records.after_detect(
                ctx.store, job, found, ctx.worktree, proposal.pipeline_secrets(), merged_fix=True
            )
        config = flow.update_state(config, {**values, "job": job}, as_node=DECIDE)
        result = flow.invoke(None, config)
    if not result.get("commented"):
        result.setdefault("stopped", "stale")
    return result


def _awaiting_skills(value: object) -> list[str]:
    if not isinstance(value, dict):
        return []
    if "skills" in value:
        return list(value["skills"])
    return [value["skill"]] if "skill" in value else []  # 2단계 전 체크포인트 ({"skill": "lint"})


def _rerun(values: Mapping[str, Any], bundle: str, *, blocked: str = "") -> bool:
    """그 묶음에 `/fix`로 다시 돌릴 것이 있는지.

    기계적 수정은 수정안을 내지 못하고 끝난 경우에만 다시 돌린다. 모델 묶음은 머지된 수정안 커밋이
    아니면, 실패·호출 상한뿐 아니라 지난 판단이 `skip`했거나 이미 수정안 PR이 열려 있는 커밋도
    사람이 `/fix`로 다시 시도·갱신할 수 있다 (pr-proof#149).
    """
    fix = values.get(STATE_KEYS[bundle][0])
    failed = fix is not None and not fix.ready
    if bundle != MODEL:
        return failed
    if blocked == "merged":
        return False
    decided = values.get("fix_decisions")
    has_model_candidates = decided is not None and any(
        d.method != MECHANICAL
        and not (d.action == "skip" and d.reason in fix_decision_pure.TABLE_SKIPS)
        for d in decided.decisions
    )
    return (
        failed
        or has_model_candidates
        or doc_rules.judge_left(values.get("doc_rules"))
        or pr_check_pure.judge_left(values.get("pr_checks"))
    )


def pending_fix(job: PRJob, ctx: FlowContext, saver: SqliteSaver, bundle: str) -> PendingFix | None:
    """이 커밋에 그 묶음의 재개할 수정이 있는지. 승인 대기 중이거나, 수정·전달 도중 실패해 다시
    시도할 수 있거나, 수정안을 내지 못하고 끝나 다시 돌릴 수 있다."""
    snapshot = build_graph(ctx, saver).get_state(thread_config(job))
    blocked = proposal.blocked_by_history(ctx.store, job.repo, job.pr_number, bundle, job.head_sha)[
        0
    ]
    if AWAIT_FIX in snapshot.next and snapshot.interrupts:
        if bundle in _awaiting_skills(snapshot.interrupts[0].value):
            return "awaiting"
        # 다른 묶음이 승인을 기다려도 이 묶음은 다시 돌릴 수 있다. 끝나면 다시 그 승인을 기다린다
        return "rerun" if _rerun(snapshot.values, bundle, blocked=blocked) else None
    if FIX_NODES[bundle] in snapshot.next or DELIVER_NODES[bundle] in snapshot.next:
        return "interrupted_fix"
    if snapshot.next:
        return None
    return "rerun" if _rerun(snapshot.values, bundle, blocked=blocked) else None


def pending_kind(job: PRJob, ctx: FlowContext, bundle: str) -> PendingFix | None:
    with ExitStack() as stack:
        return pending_fix(job, ctx, open_checkpointer(ctx.data_dir, stack), bundle)


def resume_fix(job: PRJob, ctx: FlowContext, approval: dict[str, Any]) -> FlowState | None:
    """멈춰 있던 그래프를 approval["skill"] 묶음의 수정부터 이어서 실행한다. 없으면 None.

    수정안을 내지 못하고 끝난 수정(검증 실패 등)은 수정 판단부터 다시 돈다. 모델 묶음은 호출
    상한으로 정하지 못한 판단을 다시 묻고 새 시도 세대로 고친다. 테스트 결과와 다른 묶음의 결과는
    그대로 둔다.
    """
    bundle = approval["skill"]
    with ctx.stack:
        saver = open_checkpointer(ctx.data_dir, ctx.stack)
        pending = pending_fix(job, ctx, saver, bundle)
        if pending is None:
            return None
        graph = build_graph(ctx, saver)
        config = thread_config(job)
        if pending == "rerun":
            result_key, requested_key = STATE_KEYS[bundle]
            values = graph.get_state(config).values
            # 호출 상한으로 남은 모델 판단이 있으면 모델 판단부터, PR 단위 판단만 남았으면 PR 단위
            # 확인부터, 아니면 수정 판단부터 다시 돈다
            after = TEST
            if doc_rules.judge_left(values.get("doc_rules")):
                after = DETECT
            elif pr_check_pure.judge_left(values.get("pr_checks")):
                after = JUDGE
            config = graph.update_state(
                config, {result_key: None, requested_key: True}, as_node=after
            )
        command = Command(resume=approval) if pending == "awaiting" else None
        result = graph.invoke(command, config)
    if ctx.store.is_stale(job):
        result.setdefault("stopped", "stale")
    return result
