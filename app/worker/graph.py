"""워커의 작업 흐름 (LangGraph, docs/DESIGN.md 5.1).

    체크아웃 → 규칙 읽기 → 검사 방법 정하기 → 위반 찾기 → 테스트 → 수정 판단 → 코멘트
        ─┬─ (고칠 위반 없음) → 끝
         ├─ (기본: 바로) 기계적 수정 → 수정안 PR ─┬→ 수정 코멘트 ─┬─ (남은 묶음 없음) → 끝
         ├─ (기본: 바로) 모델 수정 → 수정안 PR ───┘              └─ 다른 묶음의 대기·수정
         └─ 승인 대기(interrupt, `require_approval: true`) ─ /fix <묶음>

규칙은 base의 문서와 도구 설정(ruff 설정)에서 나온다. 규칙이 하나도 없으면 규칙 검사를 하지 않고
테스트로 넘어간다. 테스트 뒤에 위반마다 수정 판단(fix/skip과 이유, 4.6)을 정해 코멘트에 보인다.
`fix`로 정한 위반은 수정 방법별 묶음(`mechanical`: 도구의 수정, `model`: 모델, 3.1의 C7)으로 고치고
결정적으로 검증한 뒤 묶음마다 수정안 PR로 올린다. 파이프라인이 정한 규칙의 검사와 수정은 없다 (2.2).

묶음마다 수정은 한 번이다. 수정마다 새 worktree에서 시작한다 (앞 수정의 변경이 섞이지 않게).
`/fix`로 다시 돌리면 그 묶음은 수정 판단부터 다시 돈다. 모델 묶음은 호출 상한으로 남은 판단과
수정을 이어 한다 (4.9 명령).

각 단계 사이에서 이 작업의 커밋이 아직 PR의 최신 커밋인지 확인하고, 낡았으면 멈춘다.
그래프 상태에는 데이터만 넣고, worktree 같은 자원은 FlowContext가 들고 정리한다.
상태는 SQLite 체크포인트에 남고 thread_id는 `repo#pr#head_sha`다. 새 커밋이 오면 SHA가 달라져
멈춰 있던 그래프는 따로 지우지 않아도 쓰이지 않는다. 재개할 때 worktree는 다시 만든다 (5.6).
"""

import logging
import sqlite3
import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final, Literal, TypedDict

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.config import CallMaxima
from app.models import doc_rules as doc_rule_models
from app.models import fix as fix_models
from app.models import fix_decision as fix_decision_models
from app.models import model_fix as model_fix_models
from app.models.checker_authoring import Checked
from app.models.doc_rules import DocRules, Found, RuleOutcome, RulesRead
from app.models.fix import MECHANICAL_BUNDLE, MechanicalFix
from app.models.fix_decision import Decided, SafetyNet
from app.models.job import PRJob
from app.models.model_fix import ModelFix, Picked
from app.models.proposal import ProposalResult
from app.models.repo_config import call_limits
from app.models.test_run import PytestRun
from app.sandbox import container, docker_cli, toolcache
from app.worker import (
    base_config,
    coverage,
    decide,
    detect,
    doc_rules,
    mechanical_fix,
    model_fix,
    proposal,
    records,
    repo,
    report,
)
from app.worker.ai import claude_cli, workdir
from app.worker.base_config import BaseConfig
from app.worker.budget import CallBudget
from app.worker.coverage import DiffCoverage
from app.worker.doc_rules import Runtime as DocRuntime
from app.worker.doc_rules_report import FixView
from app.worker.github import GitHub
from app.worker.lang import python
from app.worker.repo import CHANGES_DIFF
from app.worker.state import Store

log = logging.getLogger(__name__)

Route = Literal["next", "stop"]
CHECKPOINT_DB: Final = "checkpoints.db"
AWAIT_FIX: Final = "await_fix"
FIX_COMMENT: Final = "fix_comment"
LOAD_RULES: Final = "load_rules"
AUTHOR_CHECKERS: Final = "author_checkers"
DETECT: Final = "detect"
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
    ("app.models.job", "PRJob"),
    ("app.models.test_run", "PytestRun"),
    ("app.models.test_run", "ReportSummary"),
    ("app.models.proposal", "ProposalResult"),
    ("app.worker.coverage", "DiffCoverage"),
    *doc_rule_models.STATE_TYPES,
    *fix_decision_models.STATE_TYPES,
    *model_fix_models.STATE_TYPES,
    *fix_models.STATE_TYPES,
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
) -> PytestRun:
    """테스트 단계: 이미지 준비 → Python 런타임(도구 캐시) → .venv 준비 → 작업 컨테이너에서 pytest
    → 결과.

    Docker 실패와 런타임을 받지 못함은 docker_cli.SandboxError(환경 오류), pip 실패는
    python.DependencyInstallError로 올라간다. 처리는 worker/update.py가 정한다.
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
        return python.run_tests(name, worktree, is_stale=is_stale)


class FlowState(TypedDict, total=False):
    job: PRJob
    diff_coverage: DiffCoverage | None
    rules_read: RulesRead  # 규칙 읽기 → 검사 방법 정하기 → 위반 찾기 사이에만 쓴다
    rule_checks: tuple[RuleOutcome, ...]
    doc_rules: DocRules  # 규칙 경로의 결과. 코멘트의 규칙 절은 이 값으로만 그린다
    fix_decisions: Decided  # 위반마다 수정 판단 (테스트 뒤, 4.6)
    mechanical_fix: MechanicalFix | None  # 기계적 수정 묶음의 결과. 수정안 PR은 proposals에
    mechanical_requested: bool  # 사람이 `/fix`로 이 커밋의 기계적 묶음을 승인했거나 다시 돌림
    model_fix: ModelFix | None  # 모델 수정 묶음의 결과 (4.7, 4.8)
    model_requested: bool
    test_run: PytestRun
    commented: bool
    fix_skill: str  # 승인 대기에서 `/fix`가 고른 묶음
    proposals: dict[str, ProposalResult]  # 묶음 → 수정안 PR
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
    first_call: int = 0  # 규칙 경로를 시작하기 전 마지막 호출 기록의 id
    dirty: bool = False  # 수정이 worktree를 바꿨다. 다음 수정은 새 worktree에서 한다
    changes_diff: bytes = b""  # 테스트 전에 읽은 PR의 diff (테스트 뒤에는 샌드박스가 바꿀 수 있다)
    run_tests: Callable[..., PytestRun] | None = None  # 없으면 run_pr_tests


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
        ctx.merge_base = co.merge_base
        ctx.dirty = False
        ctx.base = base_config.load(co.path, job.base_sha, ctx.max_memory_gb)
        try:  # 테스트 전에 읽어 둔다. 테스트 뒤에는 샌드박스의 PR 코드가 바꿀 수 있다
            ctx.changes_diff = (co.path / CHANGES_DIFF).read_bytes()
        except OSError:
            ctx.changes_diff = b""

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
            tool_cache=ctx.data_dir / TOOL_DIR,
            memory_gb=ctx.base.memory_gb,
            names=ctx.sandbox_names,
        )

    def checkout(state: FlowState) -> FlowState:
        enter_checkout(state["job"])
        return {}

    def doc_runtime(job: PRJob) -> DocRuntime:
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
            )
        return ctx.doc_runtime

    def doc_rules_done(result: DocRules) -> FlowState:
        ctx.doc_rules = result  # 의존성 설치가 실패해도(그래프 밖 예외) 코멘트에 보인다
        return {"doc_rules": result}

    def load_rules(state: FlowState) -> FlowState:
        """base의 규칙 문서와 도구 설정을 읽는다 (5.1 load_rules). 규칙이 없으면 여기서 끝난다."""
        job = state["job"]
        progress(job, "규칙 읽기")
        ctx.first_call = doc_rules.last_call_id(ctx.store, job.repo, job.pr_number)
        read = doc_rules.read(ctx.worktree, job.base_sha, ctx.base.repo.rules, doc_runtime(job))
        if isinstance(read, RulesRead):
            return {"rules_read": read}
        return doc_rules_done(read)

    def author_checkers(state: FlowState) -> FlowState:
        """규칙마다 도구나 검사기를 정하거나 저장한 것을 쓴다 (5.1 plan_checks, author_checker)."""
        job = state["job"]
        outcomes = doc_rules.author(state["rules_read"], doc_runtime(job))
        if isinstance(outcomes, tuple):
            return {"rule_checks": outcomes}
        return doc_rules_done(outcomes)

    def detect_rules(state: FlowState) -> FlowState:
        """채택한 도구와 검사기를 PR이 바꾼 파일에 돌린다 (5.1 detect). 바뀐 줄의 위반만 남긴다.

        사람이 닫은 수정안이 고친 줄에 표시하고(4.6 표의 첫 줄), 작성자가 고친 이전 위반과 이번
        위반을 기록한다 (4.10).
        """
        job = state["job"]
        result = doc_rules.find(
            state["rules_read"], state["rule_checks"], ctx.worktree, ctx.changes_diff,
            doc_runtime(job), since_call=ctx.first_call,
        )  # fmt: skip
        if isinstance(result, Found):
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
        return out

    def decide_fixes(state: FlowState) -> FlowState:
        """위반마다 fix/skip을 정한다 (5.1 decide). 표가 테스트 상태와 커버리지를 본다."""
        job, found = state["job"], state.get("doc_rules")
        if not isinstance(found, Found) or not found.violations:
            return {}
        ensure_checkout(job)  # `/fix`가 다시 돌릴 때는 새 프로세스다
        progress(job, "수정 판단")
        rt, run = doc_runtime(job), state["test_run"]
        decided = decide.decide(
            found, SafetyNet(run.status, flaky=bool(run.flaky)), ctx.changes_diff,
            require_covered=ctx.base.repo.fix.require_covered, store=rt.store,
            budget=rt.budget, model=rt.models.decide, ask=rt.ask,
        )  # fmt: skip
        return {"fix_decisions": decided}

    def targets(state: FlowState, bundle: str) -> Picked | None:
        """수정 판단이 그 묶음으로 정한 위반. 없으면 None."""
        found, decided = state.get("doc_rules"), state.get("fix_decisions")
        if not isinstance(found, Found) or decided is None:
            return None
        pick = (
            mechanical_fix.pick_targets if bundle == MECHANICAL else model_fix_models.pick_targets
        )
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
        rt = doc_runtime(job)
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
        """fix·model로 정한 위반을 모델로 고치고 결정적으로 검증한다 (5.1 fix_model)."""
        job, found = state["job"], state["doc_rules"]
        picked = targets(state, MODEL)
        fresh_worktree(job)
        progress(job, "모델 수정")
        rt = doc_runtime(job)
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
            run_tests=lambda: tests_for(job),
            is_stale=lambda: ctx.store.is_stale(job),
            secrets=proposal.pipeline_secrets(),
        )
        notes = model_fix.notes_for(found, picked, ctx.worktree)
        outcome = model_fix.fix_violations(
            found, picked, worktree=ctx.worktree, merge_base=ctx.merge_base,
            test_before=state["test_run"], store=rt.store, budget=rt.budget,
            models=ctx.base.repo.model, max_attempts=ctx.base.repo.fix.max_retries, tools=tools,
            generation=ctx.store.call_grants(job.repo, job.pr_number),
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
        LOAD_RULES, after_doc_step, {"next": AUTHOR_CHECKERS, "done": TEST, "stop": END}
    )
    graph.add_conditional_edges(
        AUTHOR_CHECKERS, after_doc_step, {"next": DETECT, "done": TEST, "stop": END}
    )
    graph.add_conditional_edges(DETECT, route, {"next": TEST, "stop": END})
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
    return capped or failed


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
            config = graph.update_state(
                config, {result_key: None, requested_key: True}, as_node=TEST
            )
        command = Command(resume=approval) if pending == "awaiting" else None
        result = graph.invoke(command, config)
    if ctx.store.is_stale(job):
        result.setdefault("stopped", "stale")
    return result
