"""워커의 작업 흐름 (LangGraph).

    체크아웃 → 규칙 검사(lint) → 테스트 → 코멘트 ─┬─ (제안할 수정 없음) → 끝
                                            ├─ (기본: 바로 수정)
                                            └─ 승인 대기(interrupt) ─ /fix <묶음>
                                               (require_approval: true)
        /fix mechanical → 수정(ruff) → 제안 PR ─┬→ 수정 코멘트 ─┬─ (남은 제안 없음) → 끝
        /fix model → 모델 수정 → 수정안 PR ─────┘              └─ 다른 묶음의 대기·수정

규칙 검사와 테스트 사이에 문서 규칙 경로가 돈다: 규칙 읽기 → 검사기 작성 → 위반 찾기 (5.1).
규칙이 없거나 이 경로가 실패하면 그 자리에서 테스트로 넘어간다. 테스트 뒤에 그 위반마다 수정 판단
(fix/skip과 이유, 4.6)을 정해 코멘트에 보인다. v1 lint의 수정이 끝난 뒤 `fix`·`model`로 정한
위반을 모델 수정 묶음으로 고치고(fix_model, 5.3 (b)) 수정안 PR로 올린다(deliver_model).
파이프라인이 정한 규칙의 검사와 수정은 없다 (2.2).

묶음마다 수정은 한 번이다. 수정마다 새 worktree에서 시작한다 (앞 수정의 변경이 섞이지 않게).
모델 묶음은 수정 판단부터 다시 돌아 호출 상한으로 남은 판단과 수정을 이어 한다 (4.9 명령).

각 단계 사이에서 이 작업의 커밋이 아직 PR의 최신 커밋인지 확인하고, 낡았으면 멈춘다.
그래프 상태에는 데이터만 넣고, worktree 같은 자원은 FlowContext가 들고 정리한다.
상태는 SQLite 체크포인트에 남고 thread_id는 `repo#pr#head_sha`다. 새 커밋이 오면 SHA가 달라져
멈춰 있던 그래프는 따로 지우지 않아도 쓰이지 않는다. 재개할 때 worktree는 다시 만든다 (F7).
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
from app.models import fix as fix_rules
from app.models import fix_decision as fix_decision_models
from app.models import model_fix as model_fix_models
from app.models import rejection
from app.models.checker_authoring import Checked
from app.models.doc_rules import DocRules, Found, RuleOutcome, RulesRead
from app.models.finding import SkillResult
from app.models.fix import FixOutcome
from app.models.fix_decision import Decided, SafetyNet
from app.models.job import PRJob
from app.models.model_fix import ModelFix, Picked
from app.models.proposal import ProposalResult
from app.models.repo_config import call_limits
from app.models.test_run import PytestRun
from app.sandbox import container, docker_cli, pytest_runner, venv
from app.worker import (
    base_config,
    coverage,
    decide,
    detect,
    doc_rules,
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
from app.worker.repo import CHANGES_DIFF
from app.worker.skills import lint
from app.worker.skills.lint import LintFix
from app.worker.state import Store

log = logging.getLogger(__name__)

Route = Literal["next", "stop"]
CHECKPOINT_DB: Final = "checkpoints.db"
FIX_DIR: Final = "fixes"
AWAIT_FIX: Final = "await_fix"
FIX: Final = "fix"
PROPOSE: Final = "propose"
FIX_COMMENT: Final = "fix_comment"
LOAD_RULES: Final = "load_rules"
AUTHOR_CHECKERS: Final = "author_checkers"
DETECT: Final = "detect"
TEST: Final = "test"
DECIDE: Final = "decide"
FIX_MODEL: Final = "fix_model"
DELIVER_MODEL: Final = "deliver_model"
MODEL: Final = proposal.MODEL_BUNDLE
# v1 lint와 모델 묶음 → 수정 node. `/fix <묶음>`이 승인하거나 다시 돌린다
FIX_NODES: Final[Mapping[str, str]] = {"lint": FIX, MODEL: FIX_MODEL}
# 작업을 다시 시도할 때 처음부터 하지 않고 이어 가는 node. 앞 node의 결과(검증한 패치)가 저장돼 있다
RESUMABLE: Final = frozenset({DELIVER_MODEL})
TOOL_DIR: Final = "tools"  # 도구 캐시 (data_dir 아래, venvs와 같은 자리)
# 체크포인트에서 되살릴 수 있는 상태 타입 (나머지는 langgraph가 막는다)
_STATE_TYPES: Final = (
    ("app.models.job", "PRJob"),
    ("app.models.finding", "Finding"),
    ("app.models.finding", "SkillResult"),
    ("app.models.test_run", "PytestRun"),
    ("app.models.test_run", "ReportSummary"),
    ("app.models.fix", "FixOutcome"),
    ("app.models.proposal", "ProposalResult"),
    ("app.worker.coverage", "DiffCoverage"),
    *doc_rule_models.STATE_TYPES,
    *fix_decision_models.STATE_TYPES,
    *model_fix_models.STATE_TYPES,
)


def run_pr_tests(
    worktree: Path,
    *,
    job_id: str | None = None,
    is_stale: Callable[[], bool] | None = None,
    venv_cache: Path = venv.VENV_CACHE,
    memory_gb: float = docker_cli.MEMORY_LIMIT_GB,
    names: docker_cli.Names = docker_cli.DEFAULT_NAMES,
) -> PytestRun:
    """테스트 단계: 이미지 준비 → .venv 준비 → 작업 컨테이너에서 pytest → 결과.

    Docker 실패는 docker_cli.SandboxError(환경 오류), pip 실패는 venv.DependencyInstallError로
    올라간다. 처리는 worker/update.py가 정한다.
    """
    image = docker_cli.ensure_image(names.image)
    venv_dir = venv.ensure_venv(
        worktree, image, cache_dir=venv_cache, tag=names.image, memory_gb=memory_gb
    )
    with container.running(
        worktree, venv_dir, job_id=job_id, memory_gb=memory_gb, names=names
    ) as name:
        return pytest_runner.run_tests(name, worktree, is_stale=is_stale)


class FlowState(TypedDict, total=False):
    job: PRJob
    lint: SkillResult
    diff_coverage: DiffCoverage | None
    rules_read: RulesRead  # 규칙 읽기 → 검사기 작성 → 위반 찾기 사이에만 쓴다
    rule_checks: tuple[RuleOutcome, ...]
    doc_rules: DocRules  # 문서 규칙 경로의 결과. 코멘트의 "문서 규칙" 절은 이 값으로만 그린다
    fix_decisions: Decided  # 문서 규칙 위반마다 수정 판단 (테스트 뒤, 4.6)
    model_fix: ModelFix | None  # 모델 수정 묶음의 결과 (4.7, 4.8). 수정안 PR은 proposals["model"]
    # 사람이 `/fix`로 이 커밋의 모델 묶음을 승인했거나 다시 돌리게 했다
    model_requested: bool
    test_run: PytestRun
    commented: bool
    fix_skill: str  # 지금(마지막으로) 수정하는 묶음
    fixes: dict[str, FixOutcome]  # v1 lint의 수정 결과. 한 번
    proposals: dict[str, ProposalResult]
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
    lint: SkillResult | None = None
    doc_rules: DocRules | None = None
    doc_runtime: DocRuntime | None = None
    first_call: int = 0  # 문서 규칙 경로를 시작하기 전 마지막 호출 기록의 id
    dirty: bool = False  # 수정이 worktree를 바꿨다. 다음 수정은 새 worktree에서 한다
    changes_diff: bytes = b""  # 테스트 전에 읽은 PR의 diff (테스트 뒤에는 샌드박스가 바꿀 수 있다)
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


SkillProposal = tuple[report.Proposal, int | None]


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
        """재개하면 새 프로세스라 worktree와 base 설정이 없다. 제안 상태를 다시 보려면 필요하다."""
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

    def enabled(skill: str) -> bool:
        return skill in ctx.base.repo.skills.enabled

    def tests_for(job: PRJob) -> PytestRun:
        return (ctx.run_tests or run_pr_tests)(
            ctx.worktree,
            job_id=f"{ctx.run_id}",
            is_stale=is_stale_for(job),
            venv_cache=ctx.data_dir / "venvs",
            memory_gb=ctx.base.memory_gb,
            names=ctx.sandbox_names,
        )

    def lint_for(job: PRJob, changed=None) -> SkillResult | None:
        return (ctx.run_lint or lint.run_lint)(
            ctx.worktree, ctx.base, job_id=f"{ctx.run_id}", is_stale=is_stale_for(job),
            names=ctx.sandbox_names, changed=changed,
        )  # fmt: skip

    def checkout(state: FlowState) -> FlowState:
        enter_checkout(state["job"])
        return {}

    def rules(state: FlowState) -> FlowState:
        """규칙 검사. 테스트 전에 돈다 (설치가 실패한 PR에도 규칙 결과를 보여준다)."""
        job = state["job"]
        if not enabled("lint"):
            return {}  # base의 .pipeline.yaml에서 껐다. 규칙 줄 없이 테스트만 보여준다
        progress(job, "규칙 검사")
        out: FlowState = {}
        rejected = ctx.store.rejected_targets(job.repo, job.pr_number)
        result = lint_for(job)
        if result is None:
            return {"stopped": "cancelled"}
        ctx.lint = out["lint"] = remember_rejections(result, rejected)
        return out

    def remember_rejections(
        result: SkillResult, rejected: Mapping[rejection.RejectionKey, int]
    ) -> SkillResult:
        """위반마다 줄 내용을 채우고, 사람이 닫은 제안이 고친 것과 같은 위반에 표시한다 (F10)."""
        if not result.findings:
            return result
        findings = rejection.with_line_text(result.findings, worktree_lines)
        return replace(result, findings=rejection.mark_rejected(findings, rejected))

    def worktree_lines(rel: str) -> list[str] | None:
        path = repo.worktree_file(ctx.worktree, rel)
        if path is None:
            return None
        try:
            return path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None

    def doc_runtime(job: PRJob) -> DocRuntime:
        if ctx.doc_runtime is None:
            models = ctx.base.repo.model
            limits = call_limits(models.calls, **ctx.call_maxima.model_dump())
            ctx.doc_runtime = DocRuntime(
                store=ctx.store,
                budget=CallBudget(ctx.store, job.repo, job.pr_number, job.head_sha, limits),
                models=models,
                ask=claude_cli.run_structured,
                sandbox=doc_rules.sandbox_for(ctx.data_dir / TOOL_DIR),
            )
        return ctx.doc_runtime

    def doc_rules_done(result: DocRules) -> FlowState:
        ctx.doc_rules = result  # 의존성 설치가 실패해도(그래프 밖 예외) 코멘트에 보인다
        return {"doc_rules": result}

    def load_rules(state: FlowState) -> FlowState:
        """base의 규칙 문서를 읽는다 (5.1 load_rules). 규칙이 없거나 못 읽으면 여기서 끝난다."""
        job = state["job"]
        progress(job, "문서 규칙")
        ctx.first_call = doc_rules.last_call_id(ctx.store, job.repo, job.pr_number)
        read = doc_rules.read(ctx.worktree, job.base_sha, ctx.base.repo.rules, doc_runtime(job))
        if isinstance(read, RulesRead):
            return {"rules_read": read}
        return doc_rules_done(read)

    def author_checkers(state: FlowState) -> FlowState:
        """규칙마다 검사기를 작성하거나 저장한 것을 쓴다 (5.1 plan_checks, author_checker)."""
        job = state["job"]
        outcomes = doc_rules.author(state["rules_read"], doc_runtime(job))
        if isinstance(outcomes, tuple):
            return {"rule_checks": outcomes}
        return doc_rules_done(outcomes)

    def detect_rules(state: FlowState) -> FlowState:
        """채택한 검사기를 PR이 바꾼 파일에 돌린다 (5.1 detect). 바뀐 줄의 위반만 남긴다.

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
            covered = coverage.annotate_coverage(doc.violations, run.coverage_xml, ctx.worktree)
            ctx.doc_rules = out["doc_rules"] = replace(doc, violations=covered)
        out["diff_coverage"] = (
            coverage.diff_coverage(run.coverage_xml, ctx.changes_diff) if ctx.changes_diff else None
        )
        return out

    def decide_fixes(state: FlowState) -> FlowState:
        """문서 규칙 위반마다 fix/skip을 정한다 (5.1 decide). 표가 테스트 상태와 커버리지를 본다."""
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

    def history_or(job: PRJob, skill: str) -> SkillProposal:
        history, number = proposal.blocked_by_history(
            ctx.store, job.repo, job.pr_number, skill, job.head_sha
        )
        if history:
            return history, number  # 머지해 생긴 커밋, 거절된 제안, 이미 열린 제안
        return ("approval" if ctx.base.repo.fix.require_approval else "auto"), None

    def skill_proposals(state: FlowState) -> dict[str, SkillProposal]:
        """v1 lint의 수정 제안 상태 (DESIGN.md F8 표). lint를 돌리지 않았으면 비어 있다."""
        job, run = state["job"], state["test_run"]
        if state.get("lint") is None:
            return {}
        blocker = fix_rules.lint_fix_blocker(state["lint"], run)
        return {"lint": (blocker, None) if blocker is not None else history_or(job, "lint")}

    def open_skills(state: FlowState, mode: str) -> list[str]:
        done = state.get("fixes", {})
        return [s for s, (p, _) in skill_proposals(state).items() if p == mode and s not in done]

    def model_targets(state: FlowState) -> Picked | None:
        """수정 판단이 fix·model로 정한 위반. 없으면 None."""
        found, decided = state.get("doc_rules"), state.get("fix_decisions")
        if not isinstance(found, Found) or decided is None:
            return None
        picked = model_fix_models.pick_targets(found.violations, decided.decisions)
        return picked if picked.targets else None

    def model_fix_view(state: FlowState) -> FixView | None:
        picked = model_targets(state)
        if picked is None:
            return None
        counts = {"targets": len(picked.targets), "size_capped": len(picked.size_capped)}
        done = state.get("model_fix")
        if done is not None:
            result = state.get("proposals", {}).get(proposal.MODEL_BUNDLE)
            attempt = ctx.store.fix_attempt(done.attempt_id) if done.attempt_id else None
            patch = (attempt.patch or "") if attempt is not None else ""
            return FixView(**counts, state="done", fix=done, proposal=result, patch=patch)
        job, requested = state["job"], state.get("model_requested", False)
        history, number = proposal.blocked_by_history(
            ctx.store, job.repo, job.pr_number, MODEL, job.head_sha
        )
        # `/fix`로 다시 돌리면 이 커밋의 열린 수정안 PR을 같은 브랜치로 갱신한다
        if history and not (history == "open" and requested):
            return FixView(**counts, state=history, number=number)
        if ctx.base.repo.fix.require_approval and not requested:
            return FixView(**counts, state="approval")
        return FixView(**counts, state="running")

    def awaiting(state: FlowState) -> list[str]:
        """승인을 기다리는 v1 lint와 모델 묶음."""
        view = model_fix_view(state)
        model = [MODEL] if view is not None and view.state == "approval" else []
        return open_skills(state, "approval") + model

    def render(state: FlowState) -> str:
        job = state["job"]
        return report.render_result(
            state["test_run"],
            job.head_sha,
            time.monotonic() - ctx.started,
            lint=state.get("lint"),
            base=ctx.base,
            proposals=skill_proposals(state),
            diff_coverage=state.get("diff_coverage"),
            fixes=state.get("fixes"),
            proposal_results=state.get("proposals"),
            from_sync=job.source == "sync",
            doc_rules=state.get("doc_rules"),
            checkers=doc_rules.shown_checkers(state.get("doc_rules"), ctx.store),
            decisions=state.get("fix_decisions"),
            model_fix=model_fix_view(state),
        )

    def comment(state: FlowState) -> FlowState:
        job = state["job"]
        ctx.comment_id = ctx.github.upsert_comment(
            job.repo, job.pr_number, render(state), ctx.comment_id
        )
        return {"commented": True}

    def next_fix(state: FlowState) -> str:
        """코멘트 뒤: 바로 고칠 스킬이 있으면 그 수정, 승인을 기다릴 스킬이 있으면 대기."""
        if ctx.store.is_stale(state["job"]):
            return END
        auto = open_skills(state, "auto")
        if auto:
            return FIX_NODES[auto[0]]
        view = model_fix_view(state)
        if view is not None and view.state == "running":
            return FIX_MODEL
        return AWAIT_FIX if awaiting(state) else END

    def await_fix(state: FlowState) -> FlowState:
        # 상태를 체크포인트에 남기고 멈춘다. 워커는 다른 작업을 처리한다.
        # /fix <묶음>이 오면 이 node부터 재개된다
        ensure_checkout(state["job"])
        skills = awaiting(state)
        approval = interrupt({"skills": skills, "head_sha": state["job"].head_sha})
        skill = approval.get("skill") if isinstance(approval, dict) else None
        if skill not in skills:
            return {"fix_skill": ""}
        return {"fix_skill": skill, **({"model_requested": True} if skill == MODEL else {})}

    def after_await(state: FlowState) -> str:
        return FIX_NODES.get(state.get("fix_skill") or "", END)

    def with_fix(state: FlowState, outcome: FixOutcome) -> FlowState:
        return {
            "fix_skill": outcome.skill,
            "fixes": {**state.get("fixes", {}), outcome.skill: outcome},
        }

    def fix(state: FlowState) -> FlowState:
        job = state["job"]
        fresh_worktree(job)
        progress(job, "lint 수정")
        skip = frozenset(
            key
            for f in state["lint"].findings
            if f.rejected_in is not None and (key := rejection.finding_key(f)) is not None
        )
        result = (ctx.fix_lint or lint.fix_lint)(
            ctx.worktree, ctx.base, ctx.merge_base, base_sha=job.base_sha, job_id=f"{ctx.run_id}",
            is_stale=is_stale_for(job), names=ctx.sandbox_names, skip=skip,
        )  # fmt: skip
        if result is None:
            return {"stopped": "cancelled"}
        outcome = verify_fix(job, state, result)
        if outcome is None:
            return {"stopped": "cancelled"}
        return with_fix(state, outcome)

    def verify_fix(job: PRJob, state: FlowState, result: LintFix) -> FixOutcome | None:
        """수정안을 결정적으로 검증한다. 새 커밋이 와서 중단되면 None."""
        before = state["lint"].findings
        if result.applied == 0 and not result.formatted_files:
            return FixOutcome(
                skill="lint", status="nothing_fixed", remaining=tuple(before),
                format_skipped=result.format_skipped,
            )  # fmt: skip
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
            formatted_files=result.formatted_files,
            format_skipped=result.format_skipped,
            patch_path=save_patch(job, repo.head_diff(ctx.worktree), "lint"),
        )

    def save_patch(job: PRJob, patch: str, skill: str) -> Path:
        out = ctx.data_dir / FIX_DIR / f"{job.repo.replace('/', '__')}-pr{job.pr_number}"
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{job.head_sha[:12]}-{skill}.patch"
        path.write_text(patch)
        return path

    def fix_model(state: FlowState) -> FlowState:
        """fix·model로 정한 문서 규칙 위반을 모델로 고치고 결정적으로 검증한다 (5.1 fix_model)."""
        job, found = state["job"], state["doc_rules"]
        picked = model_targets(state)
        fresh_worktree(job)
        progress(job, "문서 규칙 수정 (모델)")
        rt = doc_runtime(job)
        checks = doc_rules.saved_checks(found, rt.store)
        recheck_lint = None
        if enabled("lint") and state.get("lint") is not None:

            def recheck_lint(changed):
                return lint_for(job, changed)

        tools = model_fix.FixTools(
            run_model=workdir.run_in_model_folder,
            recheck=lambda changed, new: detect.detect(
                checks, ctx.worktree, changed, new_files=new, sandbox=rt.sandbox
            ),
            rules_rechecked=sum(1 for c in checks if isinstance(c, Checked)),
            run_tests=lambda: tests_for(job),
            recheck_lint=recheck_lint,
            is_stale=lambda: ctx.store.is_stale(job),
            guarded=(ctx.base.ruff_config,) if ctx.base.ruff_config else (),
            secrets=proposal.pipeline_secrets(),
        )
        notes = model_fix.notes_for(found, picked, ctx.worktree)
        outcome = model_fix.fix_violations(
            found, picked, worktree=ctx.worktree, merge_base=ctx.merge_base,
            test_before=state["test_run"], lint_before=state.get("lint"), store=rt.store,
            budget=rt.budget, models=ctx.base.repo.model,
            max_attempts=ctx.base.repo.fix.max_retries, tools=tools,
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
        progress(job, "수정안 PR")
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

    def propose(state: FlowState) -> FlowState:
        """검증을 통과한 수정안만 제안 PR로 올린다 (F10)."""
        skill = state["fix_skill"]
        outcome = state["fixes"][skill]
        if not outcome.ready:
            return {}
        job = state["job"]
        progress(job, "제안 PR")
        result = proposal.open_proposal(
            job, ctx.worktree, ctx.store, ctx.github, outcome, state[skill]
        )
        return {"proposals": {**state.get("proposals", {}), skill: result}}

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

    fix_targets = [AWAIT_FIX, FIX, FIX_MODEL, END]
    graph = StateGraph(FlowState)
    graph.add_node("checkout", checkout)
    graph.add_node("rules", rules)
    graph.add_node(LOAD_RULES, load_rules)
    graph.add_node(AUTHOR_CHECKERS, author_checkers)
    graph.add_node(DETECT, detect_rules)
    graph.add_node(TEST, test)
    graph.add_node(DECIDE, decide_fixes)
    graph.add_node("comment", comment)
    graph.add_node(AWAIT_FIX, await_fix)
    graph.add_node(FIX, fix)
    graph.add_node(PROPOSE, propose)
    graph.add_node(FIX_COMMENT, fix_comment)
    graph.add_node(FIX_MODEL, fix_model)
    graph.add_node(DELIVER_MODEL, deliver_model)
    graph.add_edge(START, "checkout")
    graph.add_conditional_edges("checkout", route, {"next": "rules", "stop": END})
    graph.add_conditional_edges("rules", route, {"next": LOAD_RULES, "stop": END})
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
    graph.add_conditional_edges(AWAIT_FIX, after_await, [FIX, FIX_MODEL, END])
    graph.add_conditional_edges(FIX, route, {"next": PROPOSE, "stop": END})
    graph.add_conditional_edges(PROPOSE, route, {"next": FIX_COMMENT, "stop": END})
    graph.add_conditional_edges(FIX_COMMENT, next_fix, fix_targets)
    graph.add_conditional_edges(FIX_MODEL, route, {"next": DELIVER_MODEL, "stop": END})
    graph.add_conditional_edges(DELIVER_MODEL, route, {"next": FIX_COMMENT, "stop": END})
    return graph.compile(checkpointer=checkpointer)


def run_flow(job: PRJob, ctx: FlowContext, *, retry: bool = False) -> FlowState:
    """새 검사를 처음부터 실행한다. 같은 커밋의 이전 체크포인트는 지운다 (/recheck 등).

    retry는 같은 작업을 RQ가 다시 시도하는 것이다. 지난 시도가 전달(`deliver_model`)에서 실패해
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


PendingFix = Literal["awaiting", "interrupted_fix", "interrupted_after_fix", "rerun"]


def _awaiting_skills(value: object) -> list[str]:
    if not isinstance(value, dict):
        return []
    if "skills" in value:
        return list(value["skills"])
    return [value["skill"]] if "skill" in value else []  # 3단계 전 체크포인트 ({"skill": "lint"})


def _model_rerun(values: Mapping[str, Any]) -> bool:
    """모델 묶음에 `/fix`로 다시 돌릴 것이 있는지: 수정안을 내지 못하고 끝났거나(검증 실패, 호출
    상한 등), 호출 상한에 걸려 정하지 못한 수정 판단이 있다 (4.9 명령)."""
    fix, decided = values.get("model_fix"), values.get("fix_decisions")
    capped = decided is not None and any(d.reason == "call_limit" for d in decided.decisions)
    return capped or (fix is not None and not fix.ready)


def pending_fix(job: PRJob, ctx: FlowContext, saver: SqliteSaver, skill: str) -> PendingFix | None:
    """이 커밋에 skill(v1 스킬 또는 모델 묶음)의 재개할 수정이 있는지. 승인 대기 중이거나,
    수정·제안·수정 코멘트 도중 실패해 다시 시도할 수 있거나, 수정안을 내지 못하고 끝나 다시 돌릴 수
    있다."""
    snapshot = build_graph(ctx, saver).get_state(thread_config(job))
    if AWAIT_FIX in snapshot.next and snapshot.interrupts:
        if skill in _awaiting_skills(snapshot.interrupts[0].value):
            return "awaiting"
        # 다른 스킬이 승인을 기다려도 모델 묶음은 다시 돌릴 수 있다. 끝나면 다시 그 승인을 기다린다
        return "rerun" if skill == MODEL and _model_rerun(snapshot.values) else None
    if FIX_NODES.get(skill) in snapshot.next or (skill == MODEL and DELIVER_MODEL in snapshot.next):
        return "interrupted_fix"
    if (PROPOSE in snapshot.next or FIX_COMMENT in snapshot.next) and snapshot.values.get(
        "fix_skill"
    ) == skill:
        return "interrupted_after_fix"
    if snapshot.next:
        return None
    if skill == MODEL:
        return "rerun" if _model_rerun(snapshot.values) else None
    done = snapshot.values.get("fixes", {}).get(skill)
    if done is not None and not done.ready:
        return "rerun"  # 끝난 수정이 수정안을 내지 못했다. /fix로 그 스킬만 다시 돌린다
    return None


def pending_kind(job: PRJob, ctx: FlowContext, skill: str) -> PendingFix | None:
    with ExitStack() as stack:
        return pending_fix(job, ctx, open_checkpointer(ctx.data_dir, stack), skill)


def resume_fix(job: PRJob, ctx: FlowContext, approval: dict[str, Any]) -> FlowState | None:
    """멈춰 있던 그래프를 approval["skill"]의 수정 단계부터 이어서 실행한다. 없으면 None.

    수정안을 내지 못하고 끝난 수정(검증 실패 등)도 그 스킬의 수정 단계부터 다시 돌린다. 모델 묶음은
    수정 판단부터 다시 돌아, 호출 상한으로 정하지 못한 판단을 다시 묻고 새 시도 세대로 고친다.
    """
    skill = approval["skill"]
    with ctx.stack:
        saver = open_checkpointer(ctx.data_dir, ctx.stack)
        pending = pending_fix(job, ctx, saver, skill)
        if pending is None:
            return None
        graph = build_graph(ctx, saver)
        config = thread_config(job)
        if pending == "interrupted_after_fix":
            # 제안 PR이나 수정 코멘트를 쓰다 실패했다. 수정한 worktree는 이미 지워졌으므로
            # 수정 단계부터 다시 한다. 같은 브랜치라 이미 연 제안 PR은 새로 만들지 않고 갱신한다.
            # fix_skill이 남아 있어 대기 node 다음 경로가 그 스킬의 수정 node로 간다
            config = graph.update_state(config, None, as_node=AWAIT_FIX)
        elif pending == "rerun" and skill == MODEL:
            # 테스트 결과와 v1 스킬의 결과는 그대로 두고 수정 판단부터 다시 간다
            config = graph.update_state(
                config, {"model_fix": None, "model_requested": True}, as_node=TEST
            )
        elif pending == "rerun":
            # 끝난 그래프의 그 스킬 결과만 지우고 대기 node 다음(그 스킬의 수정 node)부터 다시 간다.
            # 다른 스킬의 결과와 제안 PR은 그대로 두므로 수정 코멘트에 함께 남는다
            fixes = {k: v for k, v in graph.get_state(config).values["fixes"].items() if k != skill}
            config = graph.update_state(
                config, {"fixes": fixes, "fix_skill": skill}, as_node=AWAIT_FIX
            )
        command = Command(resume=approval) if pending == "awaiting" else None
        result = graph.invoke(command, config)
    stale = ctx.store.is_stale(job) if skill == MODEL else skill not in result.get("fixes", {})
    if stale:
        result.setdefault("stopped", "stale")
    return result
