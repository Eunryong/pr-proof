"""워커의 작업 흐름 (LangGraph, docs/DESIGN.md 5.1).

    체크아웃 → 규칙 읽기 → 검사 방법 정하기 → 위반 찾기 → 모델 판단 → PR 단위 확인 → PR 설명 확인
        → 테스트 → 수정 판단 → 코멘트
        ─┬─ (고칠 위반 없음) → 끝
         ├─ (기본: 바로) 기계적 수정 → 수정안 PR ─┬→ 수정 코멘트 ─┬─ (남은 묶음 없음) → 끝
         ├─ (기본: 바로) 모델 수정 → 수정안 PR ───┘              └─ 다른 묶음의 대기·수정
         └─ 승인 대기(interrupt, `require_approval: true`) ─ /fix <묶음>

규칙은 base의 문서와 도구 설정(ruff 설정)에서 나온다. 규칙이 하나도 없으면 규칙 검사를 하지 않고
PR 설명 확인과 테스트로 넘어간다. 테스트 뒤에 위반마다 수정 판단(fix/skip과 이유, 4.6)을 정해
코멘트에 보인다.
`fix`로 정한 위반은 수정 방법별 묶음(`mechanical`: 도구의 수정, `model`: 모델이 검사기와 샌드박스
테스트를 도구로 부르며 고침, 3.1의 C7)으로 고치고 출구 검사를 통과한 것만 묶음마다 수정안 PR로
올린다. 파이프라인이 정한 규칙의 검사와 수정은 없다 (2.2).

묶음마다 수정은 한 번이다. 수정마다 새 worktree에서 시작한다 (앞 수정의 변경이 섞이지 않게).
`/fix`로 다시 돌리면 그 묶음은 수정 판단부터 다시 돈다. 모델 묶음은 호출 상한으로 남은 판단과
수정을 이어 한다 (4.9 명령). 호출 상한으로 남은 모델 판단(4.5)이 있으면 모델 판단부터 다시 돌고
테스트도 다시 돌린다(새로 찾은 위반 줄의 커버 여부가 필요하다). 호출 상한으로 남은 PR 단위 판단만
있으면 PR 단위 확인부터 다시 돈다.

PR 단위 확인(4.5 끝)의 결과는 `pr_checks`에만 있고 알리기만 한다. PR 설명 확인(PR 글과 diff 견주기,
4.5 끝)의 결과는 `pr_description`에만 있고 역시 알리기만 한다. 수정 판단과 수정 묶음은
`doc_rules`(코드 위반)만 읽는다 (4-13).

각 단계 사이에서 이 작업의 커밋이 아직 PR의 최신 커밋인지 확인하고, 낡았으면 멈춘다.
그래프 상태에는 데이터만 넣고, worktree 같은 자원은 FlowContext가 들고 정리한다.
상태는 SQLite 체크포인트에 남고 thread_id는 `repo#pr#head_sha`다. 새 커밋이 오면 SHA가 달라져
멈춰 있던 그래프는 따로 지우지 않아도 쓰이지 않는다. 재개할 때 worktree는 다시 만든다 (5.6).
"""

import logging
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final, Literal, TypedDict

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.shared.config import CallMaxima
from app.shared.embed_models_pure import E5_SMALL, EMBED_MODELS
from app.shared.job_pure import PRJob
from app.worker import (
    base_config,
    coverage,
    doc_rules_pure,
    generated_tests_pure,
    mcp_pure,
    memory,
    merged_fix_pure,
    rag_pure,
    rag_switch_pure,
    records,
    repo,
    report,
)
from app.worker.ai import claude_cli, workdir
from app.worker.base_config import BaseConfig
from app.worker.budget import CallBudget
from app.worker.call_budget_pure import RulesetScope
from app.worker.checker_authoring_pure import Checked
from app.worker.coverage import DiffCoverage
from app.worker.doc_rules_pure import DocRules, Found, RuleOutcome, RulesRead
from app.worker.doc_rules_report import FixView
from app.worker.fix import (
    decide,
    fix_decision_pure,
    fix_pure,
    mechanical_fix,
    model_fix,
    model_fix_pure,
    proposal,
)
from app.worker.fix.fix_decision_pure import Decided, SafetyNet, open_targets
from app.worker.fix.fix_pure import MECHANICAL_BUNDLE, MechanicalFix
from app.worker.fix.model_fix_pure import ModelFix, Picked
from app.worker.fix.proposal_pure import ProposalResult
from app.worker.generated_tests_pure import Suite
from app.worker.github import GitHub
from app.worker.lang import python
from app.worker.merged_fix_pure import CarriedFrom
from app.worker.rag_pure import Retrieval
from app.worker.rag_switch_pure import RagSwitch
from app.worker.repo import CHANGES_DIFF
from app.worker.repo_config_pure import call_limits
from app.worker.rules import (
    detect,
    doc_rules,
    pr_check,
    pr_check_pure,
    pr_description,
    pr_description_pure,
    run_check,
    run_check_pure,
)
from app.worker.rules.doc_rules import Runtime as DocRuntime
from app.worker.rules.pr_check_pure import PrChecks, PrFacts
from app.worker.rules.pr_description_pure import PrDescription
from app.worker.sandbox import container, docker_cli, toolcache
from app.worker.state import Store
from app.worker.test_run_pure import PytestRun

log = logging.getLogger(__name__)

Route = Literal["next", "stop"]
CHECKPOINT_DB: Final = "checkpoints.db"
AWAIT_FIX: Final = "await_fix"
FIX_COMMENT: Final = "fix_comment"
LOAD_RULES: Final = "load_rules"
AUTHOR_CHECKERS: Final = "author_checkers"
DETECT: Final = "detect"
JUDGE: Final = "judge"
PR_CHECK: Final = "pr_check"
PR_DESCRIPTION: Final = "pr_description"
TEST: Final = "test"
DECIDE: Final = "decide"
FIX_MECHANICAL: Final = "fix_mechanical"
DELIVER_MECHANICAL: Final = "deliver_mechanical"
FIX_MODEL: Final = "fix_model"
DELIVER_MODEL: Final = "deliver_model"
MECHANICAL: Final = MECHANICAL_BUNDLE
MODEL: Final = proposal.MODEL_BUNDLE
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
    ("app.worker.test_run_pure", "PytestRun"),
    ("app.worker.test_run_pure", "ReportSummary"),
    ("app.worker.fix.proposal_pure", "ProposalResult"),
    ("app.worker.coverage", "DiffCoverage"),
    *doc_rules_pure.STATE_TYPES,
    *fix_decision_pure.STATE_TYPES,
    *model_fix_pure.STATE_TYPES,
    *generated_tests_pure.STATE_TYPES,
    *mcp_pure.STATE_TYPES,
    *merged_fix_pure.STATE_TYPES,
    *fix_pure.STATE_TYPES,
    *rag_pure.STATE_TYPES,
    *rag_switch_pure.STATE_TYPES,
    *pr_check_pure.STATE_TYPES,
    *pr_description_pure.STATE_TYPES,
)


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
) -> PytestRun:
    """테스트 단계: 이미지 준비 → Python 런타임(도구 캐시) → .venv 준비 → 작업 컨테이너에서 pytest
    → 결과.

    Docker 실패와 런타임을 받지 못함은 docker_cli.SandboxError(환경 오류), pip 실패는
    python.DependencyInstallError로 올라간다. 처리는 app/worker/update.py가 정한다.
    """
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
    fix_decisions: Decided  # 위반마다 수정 판단 (테스트 뒤, 4.6)
    mechanical_fix: MechanicalFix | None  # 기계적 수정 묶음의 결과. 수정안 PR은 proposals에
    mechanical_requested: bool  # 사람이 `/fix`로 이 커밋의 기계적 묶음을 승인했거나 다시 돌림
    model_fix: ModelFix | None  # 모델 수정 묶음의 결과 (4.7, 4.8)
    model_requested: bool
    test_run: PytestRun
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
    # 이 작업에서 읽은 PR 사실 (PR 단위 확인과 PR 설명 확인이 같이 쓴다. 그래프 상태에 넣지 않는다)
    pr_facts: PrFacts | str | None = None
    first_call: int = 0  # 규칙 경로를 시작하기 전 마지막 호출 기록의 id
    dirty: bool = False  # 수정이 worktree를 바꿨다. 다음 수정은 새 worktree에서 한다
    changes_diff: bytes = b""  # 테스트 전에 읽은 PR의 diff (테스트 뒤에는 샌드박스가 바꿀 수 있다)
    run_tests: Callable[..., PytestRun] | None = None  # 없으면 run_pr_tests
    rag_model: str = E5_SMALL  # 파이프라인 쪽 `rag.embed_model`. 저장소가 검색을 켰을 때만 쓴다


def _is_stale_for(ctx: FlowContext, job: PRJob) -> Callable[[], bool]:
    def is_stale() -> bool:
        ctx.store.heartbeat(ctx.run_id)  # 10초마다 불리므로 생존 확인도 같이 남긴다
        return ctx.store.is_stale(job)

    return is_stale


def pr_tests(
    ctx: FlowContext, job: PRJob, worktree: Path | None = None, *, contexts: bool = False
) -> PytestRun:
    """ctx의 worktree(또는 worktree)에서 PR의 테스트를 샌드박스로 돌린다. 테스트 node와 수정 단계,
    코멘트 수정(4.11)이 같이 쓴다."""
    kw = {"contexts": True} if contexts else {}
    return (ctx.run_tests or run_pr_tests)(
        worktree or ctx.worktree,
        job_id=f"{ctx.run_id}",
        is_stale=_is_stale_for(ctx, job),
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


def thread_id(repo_name: str, pr_number: int, head_sha: str) -> str:
    return f"{repo_name}#{pr_number}#{head_sha}"


def thread_config(job: PRJob) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id(job.repo, job.pr_number, job.head_sha)}}


def open_checkpointer(data_dir: Path, stack: ExitStack) -> SqliteSaver:
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(data_dir / CHECKPOINT_DB, check_same_thread=False)
    stack.callback(conn.close)
    return SqliteSaver(conn, serde=JsonPlusSerializer(allowed_msgpack_modules=list(_STATE_TYPES)))


def _enter_checkout(ctx: FlowContext, job: PRJob) -> None:
    co = ctx.stack.enter_context(
        repo.checkout(ctx.data_dir, job.repo, job.pr_number, job.head_sha, job.base_sha)
    )
    ctx.worktree = co.path
    ctx.merge_base = co.merge_base
    ctx.dirty = False
    ctx.base = base_config.load(co.path, job.base_sha, ctx.max_memory_gb)
    try:  # 테스트 전에 읽어 둔다. 테스트 뒤에는 샌드박스의 PR 코드가 바꿀 수 있다
        ctx.changes_diff = (co.path / CHANGES_DIFF).read_bytes()
    except OSError:
        ctx.changes_diff = b""


def build_graph(ctx: FlowContext, checkpointer: SqliteSaver | None = None):
    def enter_checkout(job: PRJob) -> None:
        _enter_checkout(ctx, job)

    def ensure_checkout(job: PRJob) -> None:
        """재개하면 새 프로세스라 worktree와 base 설정이 없다. 수정안 상태를 보려면 필요하다."""
        if ctx.base is None:
            enter_checkout(job)

    def fresh_worktree(job: PRJob) -> None:
        """수정 단계의 worktree. 재개했거나(지워졌다) 앞 수정이 바꿨으면 새로 만든다."""
        if ctx.worktree is None or ctx.dirty:
            enter_checkout(job)
        ctx.dirty = True

    def progress(job: PRJob, step: str) -> None:
        ctx.comment_id = ctx.github.upsert_comment(
            job.repo, job.pr_number, report.render_progress(job.head_sha, step), ctx.comment_id
        )

    def tests_for(job: PRJob, worktree: Path | None = None, *, contexts: bool = False) -> PytestRun:
        return pr_tests(ctx, job, worktree, contexts=contexts)

    def suite_for(job: PRJob, generated: Mapping[str, bytes] | None, files: Sequence[str]) -> Suite:
        return pr_suite(ctx, job, generated, files)

    def checkout(state: FlowState) -> FlowState:
        enter_checkout(state["job"])
        return {}

    def rag_switch(state: FlowState) -> RagSwitch:
        """이 검사의 검색 상태. 규칙 읽기에서 한 번 정해 상태에 두고, 재개한 프로세스도 그 값을
        쓴다. 상태에 없으면(이 값이 생기기 전의 체크포인트) 지금 정한다 (6.7)."""
        saved = state.get("rag_switch")
        if saved is not None:
            return saved
        return memory.switch_for(ctx.store, state["job"].repo, ctx.base.repo.rag.enabled)

    def doc_runtime(state: FlowState) -> DocRuntime:
        job = state["job"]
        if ctx.doc_runtime is None:
            models = ctx.base.repo.model
            limits = call_limits(models.calls, **ctx.call_maxima.model_dump())
            ctx.doc_runtime = DocRuntime(
                store=ctx.store,
                budget=CallBudget(ctx.store, job.repo, job.pr_number, job.head_sha, limits),
                models=models,
                ask=claude_cli.run_structured,
                sandbox=doc_rules.sandbox_for(
                    ctx.data_dir / TOOL_DIR, ctx.base.ruff, ctx.sandbox_names.image
                ),
                ruff=ctx.base.ruff,
                disabled=frozenset(ctx.base.repo.rules.disabled),
                retriever=retriever_for(job, rag_switch(state)),
                secrets=tuple(proposal.pipeline_secrets()),
            )
        return ctx.doc_runtime

    def retriever_for(job: PRJob, switch: RagSwitch) -> memory.Retriever | None:
        """검색이 켜진 검사만 검색한다 (6.5, 6.7). 꺼져 있으면 memory의 검색 함수를 부르지 않으므로
        chromadb도 임베딩 모델도 쓰지 않는다 (완료 기준 5-8)."""
        if not switch.on:
            return None
        return memory.open_retriever(
            ctx.data_dir, ctx.store, job.repo, ctx.base.repo.rag, EMBED_MODELS[ctx.rag_model]
        )

    def doc_rules_done(result: DocRules) -> FlowState:
        ctx.doc_rules = result  # 의존성 설치가 실패해도(그래프 밖 예외) 코멘트에 보인다
        return {"doc_rules": result}

    def load_rules(state: FlowState) -> FlowState:
        """base의 규칙 문서와 도구 설정을 읽는다 (5.1 load_rules). 규칙이 없으면 여기서 끝난다."""
        job = state["job"]
        progress(job, "규칙 읽기")
        ctx.first_call = doc_rules.last_call_id(ctx.store, job.repo, job.pr_number)
        switch = rag_switch(state)
        rt = doc_runtime({**state, "rag_switch": switch})
        if rt.retriever is not None:  # 검색을 켠 첫 검사에서 폴더를 만들고 밀린 기록을 넣는다 (6.5)
            rt.retriever.prepare()
        read = doc_rules.read(ctx.worktree, job.base_sha, ctx.base.repo.rules, rt, ctx.changes_diff)
        if isinstance(read, RulesRead):
            return {"rules_read": read, "rag_switch": switch}
        return {**doc_rules_done(read), "rag_switch": switch}

    def author_checkers(state: FlowState) -> FlowState:
        """규칙마다 도구나 검사기를 정하거나 저장한 것을 쓴다 (5.1 plan_checks, author_checker)."""
        job = state["job"]
        used: list[Retrieval] = []
        outcomes = doc_rules.author(
            state["rules_read"], doc_runtime(state), is_stale=lambda: ctx.store.is_stale(job),
            on_retrieval=used.append,
        )  # fmt: skip
        if isinstance(outcomes, tuple):
            return {"rule_checks": outcomes, "checker_retrievals": tuple(used)}
        return doc_rules_done(outcomes)

    def detect_rules(state: FlowState) -> FlowState:
        """채택한 도구와 검사기를 PR이 바꾼 파일에 돌린다 (5.1 detect). 바뀐 줄의 위반만 남긴다."""
        result = doc_rules.find(
            state["rules_read"], state["rule_checks"], ctx.worktree, ctx.changes_diff,
            doc_runtime(state), since_call=ctx.first_call,
            retrievals=state.get("checker_retrievals", ()),
        )  # fmt: skip
        return doc_rules_done(result)

    def judge_rules(state: FlowState) -> FlowState:
        """검사기를 만들지 못한 규칙을 모델이 바뀐 hunk에 대해 본다 (5.1 detect의 모델 판단, 4.5).

        그 뒤 사람이 닫은 수정안이 고친 줄에 표시하고(4.6 표의 첫 줄), 작성자가 고친 이전 위반과
        이번 위반을 기록한다 (4.10). `/fix`가 남은 모델 판단을 이어 할 때도 이 node부터 돈다.
        """
        job, result = state["job"], state.get("doc_rules")
        if not isinstance(result, Found):
            return {}
        ensure_checkout(job)  # `/fix`가 이어 할 때는 새 프로세스다
        if any(isinstance(o, doc_rules_pure.RuleJudged) for o in result.outcomes):
            progress(job, "모델 판단")
            result = doc_rules.judge(
                result, ctx.worktree, ctx.changes_diff, doc_runtime(state),
                is_stale=lambda: ctx.store.is_stale(job),
            )  # fmt: skip
        result = records.mark_rejected(ctx.store, job, result)
        merged_fix = any(
            p.state == "merged" and p.merge_commit == job.head_sha
            for p in ctx.store.proposals(job.repo, job.pr_number)
        )
        records.after_detect(
            ctx.store, job, result, ctx.worktree, proposal.pipeline_secrets(),
            merged_fix=merged_fix,
        )  # fmt: skip
        return doc_rules_done(result)

    def pr_check_rules(state: FlowState) -> FlowState:
        """확인 대상이 `pr`인 규칙을 PR 자체에 대해 본다 (4.5 끝). PR 규칙이 없으면 PR의 글을 읽지
        않는다. 가드레일(8.6)이 들인 작업에서만 이 node까지 온다. `/fix`가 남은 PR 단위 판단을 이어
        할 때도 이 node를 지난다."""
        job, found = state["job"], state.get("doc_rules")
        rules_ = pr_check.pr_rules(found)
        if not rules_:
            return {}
        ensure_checkout(job)
        progress(job, "PR 단위 확인")
        rt = doc_runtime(state)
        read = state.get("rules_read")
        scope = RulesetScope(read.key, len(read.rules)) if read is not None else RulesetScope("pr")
        facts = pr_facts(job)
        tools = pr_check.Tools(
            store=rt.store, budget=rt.budget, ask=rt.ask, sandbox=rt.sandbox,
            plan_model=rt.models.checker, judge_model=rt.models.judge, ruleset=scope,
        )  # fmt: skip
        known = len(found.violations) if isinstance(found, Found) else 0
        result = pr_check.check(
            rules_, facts, tools, known=known, is_stale=lambda: ctx.store.is_stale(job)
        )
        ctx.pr_checks = result
        return {"pr_checks": result}

    def pr_facts(job: PRJob) -> PrFacts | str:
        """이 작업의 PR 사실. 처음 부를 때 한 번 읽는다 (GitHub 조회 한 번)."""
        if ctx.pr_facts is None:
            ctx.pr_facts = pr_check.gather(
                ctx.worktree, ctx.merge_base or job.base_sha, job.head_sha, github=ctx.github,
                store=ctx.store, repo_name=job.repo, pr_number=job.pr_number,
            )  # fmt: skip
        return ctx.pr_facts

    def describe_pr(state: FlowState) -> FlowState:
        """PR 글(제목, 본문, 커밋 메시지)과 diff를 모델이 견준다 (4.5 끝). 팀 문서의 규칙과 따로
        돌고(규칙이 없어도), 알리기만 한다. 가드레일(8.6)이 들인 작업에서만 이 node까지 와서 PR 글을
        읽는다. `.pipeline.yaml`의 `pr_description.enabled: false`면 돌지 않는다. `/fix`가 앞
        node부터 다시 돌 때 이미 견준 결과가 있으면(호출 상한으로 남은 것이 아니면) 그대로 둔다."""
        job = state["job"]
        if not ctx.base.repo.pr_description.enabled:
            return {}
        saved = state.get("pr_description")
        if saved is not None and not pr_description_pure.description_left(saved):
            return {}
        ensure_checkout(job)
        progress(job, "PR 설명 확인")
        rt, found = doc_runtime(state), state.get("doc_rules")
        facts = pr_facts(job)
        pipeline = (
            () if isinstance(facts, str) else pr_description.pipeline_diffs(ctx.worktree, facts)
        )
        result = pr_description.check(
            facts, ctx.changes_diff.decode("utf-8", errors="replace"), ctx.worktree,
            pr_description.Tools(store=rt.store, budget=rt.budget, ask=rt.ask,
                                 model=rt.models.judge),
            pipeline=pipeline, known=len(found.violations) if isinstance(found, Found) else 0,
            is_stale=lambda: ctx.store.is_stale(job),
        )  # fmt: skip
        ctx.pr_description = result
        return {"pr_description": result}

    def after_doc_step(state: FlowState) -> str:
        if state.get("stopped") or ctx.store.is_stale(state["job"]):
            return "stop"
        return "done" if "doc_rules" in state else "next"

    def test(state: FlowState) -> FlowState:
        job = state["job"]
        progress(job, "테스트 실행")
        run = tests_for(job)
        if run.cancelled:
            return {"test_run": run, "stopped": "cancelled"}
        out: FlowState = {"test_run": run}
        doc = state.get("doc_rules")
        if isinstance(doc, Found) and doc.violations:  # 위반 줄이 테스트로 실행되는지 (4.5)
            covered = python.annotate_coverage(doc.violations, run.coverage_xml, ctx.worktree)
            ctx.doc_rules = out["doc_rules"] = replace(doc, violations=covered)
        out["diff_coverage"] = (
            coverage.diff_coverage(run.coverage_xml, ctx.changes_diff) if ctx.changes_diff else None
        )
        doc = out.get("doc_rules", doc)
        if isinstance(doc, Found) and run_check_pure.run_rules(doc):
            # 실행 결과의 규칙은 테스트와 커버리지를 보고 모델이 정한다 (4.5)
            progress(job, "실행 결과의 규칙")
            runs = run_check.judge_runs(
                doc, run, ctx.changes_diff, doc_runtime(state),
                is_stale=lambda: ctx.store.is_stale(job),
            )  # fmt: skip
            ctx.doc_rules = out["doc_rules"] = replace(doc, runs=runs)
        return out

    def decide_fixes(state: FlowState) -> FlowState:
        """위반마다 fix/skip을 정한다 (5.1 decide). 표가 테스트 상태와 커버리지를 본다."""
        job, found = state["job"], state.get("doc_rules")
        if not isinstance(found, Found) or not found.violations:
            return {}
        ensure_checkout(job)  # `/fix`가 다시 돌릴 때는 새 프로세스다
        progress(job, "수정 판단")
        rt, run = doc_runtime(state), state["test_run"]
        decided = decide.decide(
            found, SafetyNet(run.status, flaky=bool(run.flaky)), ctx.changes_diff, store=rt.store,
            budget=rt.budget, model=rt.models.decide, ask=rt.ask, retriever=rt.retriever,
            context_of=records.context_of(ctx.worktree),
        )  # fmt: skip
        return {"fix_decisions": decided}

    def targets(state: FlowState, bundle: str) -> Picked | None:
        """수정 판단이 그 묶음으로 정한 위반. 없으면 None."""
        found, decided = state.get("doc_rules"), state.get("fix_decisions")
        if not isinstance(found, Found) or decided is None:
            return None
        pick = mechanical_fix.pick_targets if bundle == MECHANICAL else model_fix_pure.pick_targets
        picked = pick(found.violations, decided.decisions)
        return picked if picked.targets else None

    def fix_view(state: FlowState, bundle: str) -> FixView | None:
        picked = targets(state, bundle)
        if picked is None:
            return None
        counts = {"targets": len(picked.targets), "size_capped": len(picked.size_capped)}
        result_key, requested_key = STATE_KEYS[bundle]
        done = state.get(result_key)
        if done is not None:
            result = state.get("proposals", {}).get(bundle)
            attempt = ctx.store.fix_attempt(done.attempt_id) if done.attempt_id else None
            patch = (attempt.patch or "") if attempt is not None else ""
            return FixView(**counts, state="done", fix=done, proposal=result, patch=patch)
        job, requested = state["job"], state.get(requested_key, False)
        history, number = proposal.blocked_by_history(
            ctx.store, job.repo, job.pr_number, bundle, job.head_sha
        )
        # `/fix`로 다시 돌리면 이 커밋의 열린 수정안 PR을 같은 브랜치로 갱신한다
        if history and not (history == "open" and requested):
            return FixView(**counts, state=history, number=number)
        if ctx.base.repo.fix.require_approval and not requested:
            return FixView(**counts, state="approval")
        return FixView(**counts, state="running")

    def awaiting(state: FlowState) -> list[str]:
        """승인을 기다리는 묶음."""
        return [
            b for b in BUNDLES if (v := fix_view(state, b)) is not None and v.state == "approval"
        ]

    def render(state: FlowState) -> str:
        job = state["job"]
        return report.render_result(
            state["test_run"],
            job.head_sha,
            time.monotonic() - ctx.started,
            base=ctx.base,
            diff_coverage=state.get("diff_coverage"),
            from_sync=job.source == "sync",
            doc_rules=state.get("doc_rules"),
            checkers=doc_rules.shown_checkers(state.get("doc_rules"), ctx.store),
            decisions=state.get("fix_decisions"),
            fixes={b: v for b in BUNDLES if (v := fix_view(state, b)) is not None},
            rag=state.get("rag_switch"),
            pr=state.get("pr_checks"),
            carried=state.get("carried"),
            description=state.get("pr_description"),
        )

    def comment(state: FlowState) -> FlowState:
        job = state["job"]
        ctx.comment_id = ctx.github.upsert_comment(
            job.repo, job.pr_number, render(state), ctx.comment_id
        )
        return {"commented": True}

    def next_fix(state: FlowState) -> str:
        """코멘트 뒤: 바로 고칠 묶음이 있으면 그 수정, 승인을 기다릴 묶음이 있으면 대기."""
        if ctx.store.is_stale(state["job"]):
            return END
        for bundle in BUNDLES:
            view = fix_view(state, bundle)
            if view is not None and view.state == "running":
                return FIX_NODES[bundle]
        return AWAIT_FIX if awaiting(state) else END

    def await_fix(state: FlowState) -> FlowState:
        # 상태를 체크포인트에 남기고 멈춘다. 워커는 다른 작업을 처리한다.
        # /fix <묶음>이 오면 이 node부터 재개된다
        ensure_checkout(state["job"])
        bundles = awaiting(state)
        approval = interrupt({"skills": bundles, "head_sha": state["job"].head_sha})
        bundle = approval.get("skill") if isinstance(approval, dict) else None
        if bundle not in bundles:
            return {"fix_skill": ""}
        return {"fix_skill": bundle, STATE_KEYS[bundle][1]: True}

    def after_await(state: FlowState) -> str:
        return FIX_NODES.get(state.get("fix_skill") or "", END)

    def fix_mechanical(state: FlowState) -> FlowState:
        """fix·mechanical로 정한 위반을 도구의 수정으로 고친다 (5.1 fix_mechanical, 4.7, 4.8)."""
        job, found = state["job"], state["doc_rules"]
        picked = targets(state, MECHANICAL)
        fresh_worktree(job)
        progress(job, "기계적 수정")
        rt = doc_runtime(state)
        notes = model_fix.notes_for(found, picked, ctx.worktree)
        outcome = mechanical_fix.fix_violations(
            found, picked, worktree=ctx.worktree, merge_base=ctx.merge_base,
            base_sha=job.base_sha, test_before=state["test_run"],
            checks=doc_rules.saved_checks(found, rt.store), sandbox=rt.sandbox,
            run_tests=lambda: tests_for(job), is_stale=lambda: ctx.store.is_stale(job),
            secrets=proposal.pipeline_secrets(), disabled=rt.disabled,
        )  # fmt: skip
        if outcome is None:
            return {"stopped": "cancelled"}
        outcome = mechanical_fix.save_attempt(
            ctx.store, job, ctx.merge_base, outcome, notes, ctx.worktree
        )
        records.after_failed_fix(
            ctx.store, job, outcome, notes, ctx.worktree, proposal.pipeline_secrets()
        )
        return {"mechanical_fix": outcome}

    def deliver_mechanical(state: FlowState) -> FlowState:
        """검증을 통과한 기계적 수정안만 수정안 PR로 올린다 (5.1 deliver). 늘 새 worktree에 저장한
        패치를 적용하고 파일 해시를 맞춰 본 뒤 올린다 (4.8 끝)."""
        fix = state["mechanical_fix"]
        if not fix.ready:
            return {}
        job = state["job"]
        fresh_worktree(job)
        progress(job, "수정안 PR (기계적 수정)")
        stored = model_fix.reapply(ctx.store, fix.attempt_id, ctx.worktree)
        if isinstance(stored, str):
            result = ProposalResult(reason=stored)
        else:
            result = proposal.open_mechanical_proposal(
                job, ctx.worktree, ctx.store, ctx.github, fix, state["doc_rules"],
                patch=stored.patch or "", tool=ctx.base.ruff.label,
            )  # fmt: skip
        return {"proposals": {**state.get("proposals", {}), MECHANICAL: result}}

    def fix_model(state: FlowState) -> FlowState:
        """fix·model로 정한 위반을 모델이 도구로 확인하며 고치고 출구 검사를 한다 (5.1)."""
        job, found = state["job"], state["doc_rules"]
        picked = targets(state, MODEL)
        fresh_worktree(job)
        progress(job, "모델 수정")
        rt = doc_runtime(state)
        budget = rt.budget.with_targets(open_targets(state["fix_decisions"].decisions))
        checks = doc_rules.saved_checks(found, rt.store)
        tools = model_fix.FixTools(
            run_model=workdir.run_in_model_folder,
            recheck=lambda changed, new: detect.detect(
                checks,
                ctx.worktree,
                changed,
                new_files=new,
                sandbox=rt.sandbox,
                disabled=rt.disabled,
            ),  # fmt: skip
            rules_rechecked=sum(1 for c in checks if isinstance(c, Checked)),
            run_suite=lambda generated, files: suite_for(job, generated, files),
            is_stale=lambda: ctx.store.is_stale(job),
            secrets=proposal.pipeline_secrets(),
        )
        notes = model_fix.notes_for(found, picked, ctx.worktree)
        outcome = model_fix.fix_violations(
            found, picked, worktree=ctx.worktree, merge_base=ctx.merge_base,
            test_before=state["test_run"], store=rt.store, budget=budget,
            models=ctx.base.repo.model, max_attempts=ctx.base.repo.fix.max_retries, tools=tools,
            generation=ctx.store.call_grants(job.repo, job.pr_number), retriever=rt.retriever,
            context_of=records.context_of(ctx.worktree),
        )  # fmt: skip
        if outcome is None:
            return {"stopped": "cancelled"}
        outcome = model_fix.save_attempt(
            ctx.store, job, ctx.merge_base, outcome, notes, ctx.worktree
        )
        records.after_failed_fix(
            ctx.store, job, outcome, notes, ctx.worktree, proposal.pipeline_secrets()
        )
        return {"model_fix": outcome}

    def deliver_model(state: FlowState) -> FlowState:
        """검증을 통과한 모델 수정안만 수정안 PR로 올린다 (5.1 deliver).

        늘 새 worktree에 저장한 패치(`fix_attempts`)를 적용하고 파일 해시를 맞춰 본 뒤 올린다
        (4.8 끝). 전달이 실패해 재시도가 이 node부터 이어 가도 모델을 다시 부르지 않고 같은 패치가
        올라간다 (3.1의 C6).
        """
        fix = state["model_fix"]
        if not fix.ready:
            return {}
        job = state["job"]
        fresh_worktree(job)
        progress(job, "수정안 PR (모델 수정)")
        stored = model_fix.reapply(ctx.store, fix.attempt_id, ctx.worktree)
        if isinstance(stored, str):
            result = ProposalResult(reason=stored)
        else:
            skipped = sum(1 for d in state["fix_decisions"].decisions if d.action == "skip")
            result = proposal.open_model_proposal(
                job, ctx.worktree, ctx.store, ctx.github, fix, state["doc_rules"],
                patch=stored.patch or "", skipped=skipped,
            )  # fmt: skip
        return {"proposals": {**state.get("proposals", {}), MODEL: result}}

    def fix_comment(state: FlowState) -> FlowState:
        job = state["job"]
        ctx.comment_id = ctx.github.upsert_comment(
            job.repo, job.pr_number, render(state), ctx.comment_id
        )
        return {}

    def route(state: FlowState) -> Route:
        if state.get("stopped") or ctx.store.is_stale(state["job"]):
            return "stop"
        return "next"

    fix_targets = [AWAIT_FIX, FIX_MECHANICAL, FIX_MODEL, END]
    graph = StateGraph(FlowState)
    graph.add_node("checkout", checkout)
    graph.add_node(LOAD_RULES, load_rules)
    graph.add_node(AUTHOR_CHECKERS, author_checkers)
    graph.add_node(DETECT, detect_rules)
    graph.add_node(JUDGE, judge_rules)
    graph.add_node(PR_CHECK, pr_check_rules)
    graph.add_node(PR_DESCRIPTION, describe_pr)
    graph.add_node(TEST, test)
    graph.add_node(DECIDE, decide_fixes)
    graph.add_node("comment", comment)
    graph.add_node(AWAIT_FIX, await_fix)
    graph.add_node(FIX_MECHANICAL, fix_mechanical)
    graph.add_node(DELIVER_MECHANICAL, deliver_mechanical)
    graph.add_node(FIX_MODEL, fix_model)
    graph.add_node(DELIVER_MODEL, deliver_model)
    graph.add_node(FIX_COMMENT, fix_comment)
    graph.add_edge(START, "checkout")
    graph.add_conditional_edges("checkout", route, {"next": LOAD_RULES, "stop": END})
    graph.add_conditional_edges(
        LOAD_RULES, after_doc_step, {"next": AUTHOR_CHECKERS, "done": PR_DESCRIPTION, "stop": END}
    )
    graph.add_conditional_edges(
        AUTHOR_CHECKERS, after_doc_step, {"next": DETECT, "done": PR_DESCRIPTION, "stop": END}
    )
    graph.add_conditional_edges(DETECT, route, {"next": JUDGE, "stop": END})
    graph.add_conditional_edges(JUDGE, route, {"next": PR_CHECK, "stop": END})
    graph.add_conditional_edges(PR_CHECK, route, {"next": PR_DESCRIPTION, "stop": END})
    graph.add_conditional_edges(PR_DESCRIPTION, route, {"next": TEST, "stop": END})
    graph.add_conditional_edges(TEST, route, {"next": DECIDE, "stop": END})
    graph.add_conditional_edges(DECIDE, route, {"next": "comment", "stop": END})
    graph.add_conditional_edges("comment", next_fix, fix_targets)
    graph.add_conditional_edges(AWAIT_FIX, after_await, [FIX_MECHANICAL, FIX_MODEL, END])
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
        _enter_checkout(ctx, job)
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


PendingFix = Literal["awaiting", "interrupted_fix", "rerun"]


def _awaiting_skills(value: object) -> list[str]:
    if not isinstance(value, dict):
        return []
    if "skills" in value:
        return list(value["skills"])
    return [value["skill"]] if "skill" in value else []  # 2단계 전 체크포인트 ({"skill": "lint"})


def _rerun(values: Mapping[str, Any], bundle: str) -> bool:
    """그 묶음에 `/fix`로 다시 돌릴 것이 있는지: 수정안을 내지 못하고 끝났다(검증 실패, 호출 상한
    등). 모델 묶음은 호출 상한에 걸려 정하지 못한 수정 판단도 다시 돌린다 (4.9 명령)."""
    fix = values.get(STATE_KEYS[bundle][0])
    failed = fix is not None and not fix.ready
    if bundle != MODEL:
        return failed
    decided = values.get("fix_decisions")
    capped = decided is not None and any(d.reason == "call_limit" for d in decided.decisions)
    return (
        capped
        or failed
        or doc_rules.judge_left(values.get("doc_rules"))
        or pr_check_pure.judge_left(values.get("pr_checks"))
    )


def pending_fix(job: PRJob, ctx: FlowContext, saver: SqliteSaver, bundle: str) -> PendingFix | None:
    """이 커밋에 그 묶음의 재개할 수정이 있는지. 승인 대기 중이거나, 수정·전달 도중 실패해 다시
    시도할 수 있거나, 수정안을 내지 못하고 끝나 다시 돌릴 수 있다."""
    snapshot = build_graph(ctx, saver).get_state(thread_config(job))
    if AWAIT_FIX in snapshot.next and snapshot.interrupts:
        if bundle in _awaiting_skills(snapshot.interrupts[0].value):
            return "awaiting"
        # 다른 묶음이 승인을 기다려도 이 묶음은 다시 돌릴 수 있다. 끝나면 다시 그 승인을 기다린다
        return "rerun" if _rerun(snapshot.values, bundle) else None
    if FIX_NODES[bundle] in snapshot.next or DELIVER_NODES[bundle] in snapshot.next:
        return "interrupted_fix"
    if snapshot.next:
        return None
    return "rerun" if _rerun(snapshot.values, bundle) else None


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
