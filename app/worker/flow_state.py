"""작업 흐름의 상태, 컨텍스트, 체크포인트, 공용 실행 도우미 (docs/DESIGN.md 5.5, 5.6).

그래프 노드(`app/worker/flow_nodes.py`)와 그래프 조립·실행(`app/worker/graph.py`)이 같이 쓰는
데이터 정의와 런타임 도우미를 둔다.
"""

import logging
import sqlite3
import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, TypedDict

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

from app.shared.config import CallMaxima
from app.shared.embed_models_pure import E5_SMALL, EMBED_MODELS
from app.shared.job_pure import PRJob
from app.worker.config import base_config
from app.worker.config.access_pure import Author
from app.worker.config.base_config import BaseConfig
from app.worker.config.repo_config_pure import call_limits
from app.worker.core.bundle_pure import MECHANICAL_BUNDLE, MODEL_BUNDLE
from app.worker.fix import (
    fix_decision_pure,
    fix_pure,
    merged_fix_pure,
    model_fix_pure,
    proposal,
)
from app.worker.fix.fix_decision_pure import Decided
from app.worker.fix.fix_pure import MechanicalFix
from app.worker.fix.merged_fix_pure import CarriedFrom
from app.worker.fix.model_fix_pure import ModelFix
from app.worker.fix.proposal_pure import ProposalResult
from app.worker.git import repo
from app.worker.git.github import GitHub
from app.worker.git.repo import CHANGES_DIFF
from app.worker.model import mcp_pure
from app.worker.model.model_backend import ModelBackend
from app.worker.report import report
from app.worker.rules import (
    doc_rules,
    doc_rules_pure,
    linked_issues_pure,
    pr_check_pure,
    pr_description_pure,
)
from app.worker.rules.doc_rules import Runtime as DocRuntime
from app.worker.rules.doc_rules_pure import DocRules, RuleOutcome, RulesRead
from app.worker.rules.linked_issues_pure import LinkedIssues
from app.worker.rules.pr_check_pure import PrChecks, PrFacts
from app.worker.rules.pr_description_pure import PrDescription
from app.worker.sandbox import docker_cli
from app.worker.store import memory, rag_pure, rag_switch_pure
from app.worker.store.budget import CallBudget
from app.worker.store.rag_pure import Retrieval
from app.worker.store.rag_switch_pure import RagSwitch
from app.worker.store.state import Store
from app.worker.testing import generated_tests_pure, test_run_pure
from app.worker.testing.coverage import DiffCoverage
from app.worker.testing.test_run_pure import TestRun

log = logging.getLogger(__name__)

Route = Literal["next", "stop"]
PendingFix = Literal["awaiting", "interrupted_fix", "rerun"]

CHECKPOINT_DB: Final = "checkpoints.db"
AWAIT_FIX: Final = "await_fix"
FIX_COMMENT: Final = "fix_comment"
LOAD_RULES: Final = "load_rules"
AUTHOR_CHECKERS: Final = "author_checkers"
DETECT: Final = "detect"
JUDGE: Final = "judge"
PR_CHECK: Final = "pr_check"
PR_DESCRIPTION: Final = "pr_description"
LINKED_ISSUES: Final = "linked_issues"
TEST: Final = "test"
DECIDE: Final = "decide"
FIX_MECHANICAL: Final = "fix_mechanical"
DELIVER_MECHANICAL: Final = "deliver_mechanical"
FIX_MODEL: Final = "fix_model"
DELIVER_MODEL: Final = "deliver_model"
MECHANICAL: Final = MECHANICAL_BUNDLE
MODEL: Final = MODEL_BUNDLE
BUNDLES: Final = (MECHANICAL, MODEL)  # 고치는 순서. 싼 쪽부터 (9.3)
# 묶음 → 수정 node, 전달 node. `/fix <묶음>`이 승인하거나 다시 돌린다
FIX_NODES: Final[Mapping[str, str]] = {MECHANICAL: FIX_MECHANICAL, MODEL: FIX_MODEL}
DELIVER_NODES: Final[Mapping[str, str]] = {MECHANICAL: DELIVER_MECHANICAL, MODEL: DELIVER_MODEL}
# 묶음 → (결과의 상태 키, `/fix`로 승인했거나 다시 돌리게 한 표시의 상태 키)
STATE_KEYS: Final[Mapping[str, tuple[str, str]]] = {
    MECHANICAL: ("mechanical_fix", "mechanical_requested"),
    MODEL: ("model_fix", "model_requested"),
}
# 작업을 다시 시도할 때 처음부터 하지 않고 이어 가는 node. 앞 node의 결과(검증한 패치)가 저장돼 있다
RESUMABLE: Final = frozenset({DELIVER_MECHANICAL, DELIVER_MODEL})
TOOL_DIR: Final = "tools"  # 도구 캐시 (data_dir 아래, venvs와 같은 자리)
# 체크포인트에서 되살릴 수 있는 상태 타입 (나머지는 langgraph가 막는다)
_STATE_TYPES: Final = (
    ("app.shared.job_pure", "PRJob"),
    ("app.worker.fix.proposal_pure", "ProposalResult"),
    ("app.worker.testing.coverage", "DiffCoverage"),
    *doc_rules_pure.STATE_TYPES,
    *fix_decision_pure.STATE_TYPES,
    *model_fix_pure.STATE_TYPES,
    *generated_tests_pure.STATE_TYPES,
    *test_run_pure.STATE_TYPES,
    *mcp_pure.STATE_TYPES,
    *merged_fix_pure.STATE_TYPES,
    *fix_pure.STATE_TYPES,
    *rag_pure.STATE_TYPES,
    *rag_switch_pure.STATE_TYPES,
    *pr_check_pure.STATE_TYPES,
    *pr_description_pure.STATE_TYPES,
    *linked_issues_pure.STATE_TYPES,
)


class FlowState(TypedDict, total=False):
    job: PRJob
    diff_coverage: DiffCoverage | None
    rules_read: RulesRead  # 규칙 읽기 → 검사 방법 정하기 → 위반 찾기 사이에만 쓴다
    rule_checks: tuple[RuleOutcome, ...]
    checker_retrievals: tuple[Retrieval, ...]  # 검사기 작성에 넣은 과거 기록 (Found로 옮긴다)
    rag_switch: RagSwitch  # 이 검사의 검색 상태와 이유 (규칙 읽기에서 정함, 6.7)
    doc_rules: DocRules  # 규칙 경로의 결과. 코멘트의 규칙 절은 이 값으로만 그린다
    # PR 단위 확인의 결과 (4.5 끝). PR 규칙이 있는 검사만. 알리기만 하고 수정 경로는 읽지 않는다
    pr_checks: PrChecks
    # PR 설명 확인의 결과 (4.5 끝). `.pipeline.yaml`로 끄지 않은 검사만. 알리기만 한다
    pr_description: PrDescription
    # 이슈 확인의 결과 (4.5 끝). `.pipeline.yaml`로 끄지 않은 검사만. 알리기만 한다
    linked_issues: LinkedIssues
    fix_decisions: Decided  # 위반마다 수정 판단 (테스트 뒤, 4.6)
    mechanical_fix: MechanicalFix | None  # 기계적 수정 묶음의 결과. 수정안 PR은 proposals에
    mechanical_requested: bool  # 사람이 `/fix`로 이 커밋의 기계적 묶음을 승인했거나 다시 돌림
    model_fix: ModelFix | None  # 모델 수정 묶음의 결과 (4.7, 4.8)
    model_requested: bool
    test_run: TestRun
    commented: bool
    fix_skill: str  # 승인 대기에서 `/fix`가 고른 묶음
    proposals: dict[str, ProposalResult]  # 묶음 → 수정안 PR
    # 수정안을 머지한 커밋이라 검사하지 않고 검증한 결과를 옮겨 적었다 (`carry_flow`)
    carried: CarriedFrom
    stopped: Literal["stale", "cancelled"]


@dataclass
class FlowContext:
    store: Store
    github: GitHub
    data_dir: Path
    run_id: int
    comment_id: int | None
    max_memory_gb: float
    backend: ModelBackend  # 모델 구현 (`app/worker/model/model_backend.py`). 워커가 작업마다 넘긴다
    call_maxima: CallMaxima = field(default_factory=CallMaxima)
    sandbox_names: docker_cli.Names = docker_cli.DEFAULT_NAMES
    started: float = field(default_factory=time.monotonic)
    stack: ExitStack = field(default_factory=ExitStack)
    worktree: Path | None = None
    merge_base: str | None = None  # PR이 바꾼 줄을 셀 기준 (repo.Checkout.merge_base)
    base: BaseConfig | None = None
    # 의존성 설치가 실패해도(그래프 밖 예외) 코멘트에 규칙 결과를 쓰려고 여기에도 둔다
    doc_rules: DocRules | None = None
    doc_runtime: DocRuntime | None = None
    pr_checks: PrChecks | None = None  # doc_rules와 같은 까닭으로 둔다
    pr_description: PrDescription | None = None  # doc_rules와 같은 까닭으로 둔다
    linked_issues: LinkedIssues | None = None  # doc_rules와 같은 까닭으로 둔다
    # 이 작업에서 읽은 PR 사실 (PR 단위 확인과 PR 설명 확인이 같이 쓴다. 그래프 상태에 넣지 않는다)
    pr_facts: PrFacts | str | None = None
    first_call: int = 0  # 규칙 경로를 시작하기 전 마지막 호출 기록의 id
    dirty: bool = False  # 수정이 worktree를 바꿨다. 다음 수정은 새 worktree에서 한다
    changes_diff: bytes = b""  # 테스트 전에 읽은 PR의 diff (테스트 뒤에는 샌드박스가 바꿀 수 있다)
    run_tests: Callable[..., TestRun] | None = None  # 없으면 run_pr_tests
    rag_model: str = E5_SMALL  # 파이프라인 쪽 `rag.embed_model`. 저장소가 검색을 켰을 때만 쓴다
    # 파이프라인 쪽 `rag.allowed` (`pr-proof rag on|off`). False면 이 컴퓨터에서 검색하지 않는다
    rag_allowed: bool | None = None
    # 파이프라인 쪽 `model.default`. 저장소의 `.pipeline.yaml`이 적지 않은 단계의 모델이다
    default_model: str | None = None
    # 이 작업의 승인 확인이 GitHub에서 읽은 PR 작성자. 규칙 문서를 PR의 것으로 읽을지 정하는 데
    # 쓴다. 없으면 믿지 않는 작성자로 본다
    author: Author | None = None


def timed(
    ctx: FlowContext, name: str, node: Callable[[Any], Any],
    clock: Callable[[], float] = time.monotonic,
) -> Callable[[Any], Any]:  # fmt: skip
    """단계가 끝날 때(실패해 예외로 나갈 때도) 걸린 시간을 로그와 그 검사의 줄에 남긴다. 시간을
    남기다 실패하면 로그에만 적는다. 단계의 결과와 단계가 낸 예외는 그대로 나간다."""

    def run(state: Any) -> Any:
        start = clock()
        try:
            return node(state)
        finally:
            seconds = clock() - start
            log.info("단계 %s: %.1f초", name, seconds)
            try:
                ctx.store.add_stage_time(ctx.run_id, name, seconds)
            except Exception:  # noqa: BLE001 (기록의 실패가 검사를 바꾸지 않는다)
                log.warning("단계 %s의 시간을 남기지 못함", name, exc_info=True)

    return run


def is_stale_for(ctx: FlowContext, job: PRJob) -> Callable[[], bool]:
    def is_stale() -> bool:
        ctx.store.heartbeat(ctx.run_id)  # 10초마다 불리므로 생존 확인도 같이 남긴다
        return ctx.store.is_stale(job)

    return is_stale


def thread_id(repo_name: str, pr_number: int, head_sha: str) -> str:
    return f"{repo_name}#{pr_number}#{head_sha}"


def thread_config(job: PRJob) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id(job.repo, job.pr_number, job.head_sha)}}


def open_checkpointer(data_dir: Path, stack: ExitStack) -> SqliteSaver:
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(data_dir / CHECKPOINT_DB, check_same_thread=False)
    stack.callback(conn.close)
    return SqliteSaver(conn, serde=JsonPlusSerializer(allowed_msgpack_modules=list(_STATE_TYPES)))


def enter_checkout(ctx: FlowContext, job: PRJob) -> None:
    co = ctx.stack.enter_context(
        repo.checkout(ctx.data_dir, job.repo, job.pr_number, job.head_sha, job.base_sha)
    )
    ctx.worktree = co.path
    ctx.merge_base = co.merge_base
    ctx.dirty = False
    ctx.base = base_config.load(
        co.path, job.base_sha, ctx.max_memory_gb, default_model=ctx.default_model
    )
    try:  # 테스트 전에 읽어 둔다. 테스트 뒤에는 샌드박스의 PR 코드가 바꿀 수 있다
        ctx.changes_diff = (co.path / CHANGES_DIFF).read_bytes()
    except OSError:
        ctx.changes_diff = b""


def ensure_checkout(ctx: FlowContext, job: PRJob) -> None:
    """재개하면 새 프로세스라 worktree와 base 설정이 없다. 수정안 상태를 보려면 필요하다."""
    if ctx.base is None:
        enter_checkout(ctx, job)


def fresh_worktree(ctx: FlowContext, job: PRJob) -> None:
    """수정 단계의 worktree. 재개했거나(지워졌다) 앞 수정이 바꿨으면 새로 만든다."""
    if ctx.worktree is None or ctx.dirty:
        enter_checkout(ctx, job)
    ctx.dirty = True


def progress(ctx: FlowContext, job: PRJob, step: str) -> None:
    ctx.comment_id = ctx.github.upsert_comment(
        job.repo, job.pr_number, report.render_progress(job.head_sha, step), ctx.comment_id,
        head_sha=job.head_sha,
    )  # fmt: skip


def rag_switch(ctx: FlowContext, state: FlowState) -> RagSwitch:
    """이 검사의 검색 상태. 규칙 읽기에서 한 번 정해 상태에 두고, 재개한 프로세스도 그 값을
    쓴다. 상태에 없으면(이 값이 생기기 전의 체크포인트) 지금 정한다 (6.7)."""
    saved = state.get("rag_switch")
    if saved is not None:
        return saved
    return memory.switch_for(
        ctx.store, state["job"].repo, ctx.base.repo.rag.enabled, allowed=ctx.rag_allowed
    )


def retriever_for(ctx: FlowContext, job: PRJob, switch: RagSwitch) -> memory.Retriever | None:
    """검색이 켜진 검사만 검색한다 (6.5, 6.7). 꺼져 있으면 memory의 검색 함수를 부르지 않으므로
    chromadb도 임베딩 모델도 쓰지 않는다 (완료 기준 5-8)."""
    if not switch.on:
        return None
    return memory.open_retriever(
        ctx.data_dir, ctx.store, job.repo, ctx.base.repo.rag, EMBED_MODELS[ctx.rag_model]
    )


def doc_runtime(ctx: FlowContext, state: FlowState) -> DocRuntime:
    job = state["job"]
    if ctx.doc_runtime is None:
        models = ctx.base.repo.model
        limits = call_limits(models.calls, **ctx.call_maxima.model_dump())
        ctx.doc_runtime = DocRuntime(
            store=ctx.store,
            budget=CallBudget(ctx.store, job.repo, job.pr_number, job.head_sha, limits),
            models=models,
            backend=ctx.backend,
            sandbox=doc_rules.sandbox_for(
                ctx.data_dir / TOOL_DIR, ctx.base.ruff, ctx.sandbox_names.image
            ),
            ruff=ctx.base.ruff,
            disabled=frozenset(ctx.base.repo.rules.disabled),
            retriever=retriever_for(ctx, job, rag_switch(ctx, state)),
            secrets=tuple(proposal.pipeline_secrets()),
        )
    return ctx.doc_runtime
