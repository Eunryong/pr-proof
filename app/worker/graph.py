"""워커의 작업 흐름 (LangGraph).

    체크아웃 → 규칙 검사(lint, static) → 테스트 → 코멘트 ─┬─ (제안할 수정 없음) → 끝
                                                   ├─ (기본: 바로 수정)
                                                   └─ 승인 대기(interrupt) ─ /fix <skill>
                                                      (require_approval: true)
        /fix lint   → 수정(ruff) ─┐
        /fix static → AI 수정 ────┴→ 제안 PR → 수정 코멘트 ─┬─ (남은 제안 없음) → 끝
                                                       └─ 다른 스킬의 승인 대기·자동 수정

스킬마다 수정은 한 번이다. 수정마다 새 worktree에서 시작한다 (앞 수정의 변경이 섞이지 않게).
수정안을 내지 못하고 끝난 스킬은 `/fix <skill>`로 그 스킬만 다시 돌린다 (사람이 달 때만).

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

from app.models import fix as fix_rules
from app.models import proposal as proposal_rules
from app.models import rejection
from app.models.coverage import FixGate, code_fix_gate
from app.models.db import AICall
from app.models.finding import SkillResult
from app.models.fix import FixOutcome
from app.models.job import PRJob
from app.models.proposal import ProposalResult
from app.models.test_run import PytestRun
from app.sandbox import container, docker_cli, pytest_runner, venv
from app.worker import ai_fix, base_config, coverage, proposal, repo, report
from app.worker.ai import claude_cli
from app.worker.ai.claude_cli import ClaudeResult
from app.worker.base_config import BaseConfig
from app.worker.coverage import DiffCoverage
from app.worker.github import GitHub
from app.worker.repo import CHANGES_DIFF
from app.worker.skills import lint, static
from app.worker.skills.lint import LintFix
from app.worker.skills.static import DEFAULT_SOURCE as STATIC_DEFAULT_SOURCE
from app.worker.skills.static_rules import DEFAULT_MIN_REPEATS
from app.worker.state import Store

log = logging.getLogger(__name__)

Route = Literal["next", "stop"]
CHECKPOINT_DB: Final = "checkpoints.db"
FIX_DIR: Final = "fixes"
AWAIT_FIX: Final = "await_fix"
FIX: Final = "fix"
AI_FIX: Final = "ai_fix"
FIX_NODES: Final[Mapping[str, str]] = {"lint": FIX, "static": AI_FIX}  # 스킬 → 수정 node
PROPOSE: Final = "propose"
FIX_COMMENT: Final = "fix_comment"
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
    static: SkillResult  # 테스트 뒤에는 위반마다 covered가 채워져 있다
    diff_coverage: DiffCoverage | None
    test_run: PytestRun
    commented: bool
    fix_skill: str  # 지금(마지막으로) 수정하는 스킬
    fixes: dict[str, FixOutcome]  # 스킬별 수정 결과. 스킬마다 한 번
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
    sandbox_names: docker_cli.Names = docker_cli.DEFAULT_NAMES
    started: float = field(default_factory=time.monotonic)
    stack: ExitStack = field(default_factory=ExitStack)
    worktree: Path | None = None
    merge_base: str | None = None  # PR이 바꾼 줄을 셀 기준 (repo.Checkout.merge_base)
    base: BaseConfig | None = None
    # 의존성 설치가 실패해도(그래프 밖 예외) 코멘트에 규칙 결과를 쓰려고 여기에도 둔다
    lint: SkillResult | None = None
    static: SkillResult | None = None
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

    def static_for(job: PRJob, changed=None) -> SkillResult | None:
        min_repeats = ctx.base.repo.skills.static.min_repeats
        return static.run_static(
            ctx.worktree, ctx.base,
            min_repeats=min_repeats or DEFAULT_MIN_REPEATS,
            source=base_config.PIPELINE_YAML if min_repeats else STATIC_DEFAULT_SOURCE,
            job_id=f"{ctx.run_id}", is_stale=is_stale_for(job), names=ctx.sandbox_names,
            changed=changed,
        )  # fmt: skip

    def checkout(state: FlowState) -> FlowState:
        enter_checkout(state["job"])
        return {}

    def rules(state: FlowState) -> FlowState:
        """규칙 검사. 테스트 전에 돈다 (설치가 실패한 PR에도 규칙 결과를 보여준다)."""
        job = state["job"]
        if not (enabled("lint") or enabled("static")):
            return {}  # base의 .pipeline.yaml에서 껐다. 규칙 줄 없이 테스트만 보여준다
        progress(job, "규칙 검사")
        out: FlowState = {}
        rejected = ctx.store.rejected_targets(job.repo, job.pr_number)
        if enabled("lint"):
            result = lint_for(job)
            if result is None:
                return {"stopped": "cancelled"}
            ctx.lint = out["lint"] = remember_rejections(result, rejected)
        if enabled("static"):
            result = static_for(job)
            if result is None:
                return {"stopped": "cancelled"}
            ctx.static = out["static"] = remember_rejections(result, rejected)
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

    def test(state: FlowState) -> FlowState:
        job = state["job"]
        progress(job, "테스트 실행")
        run = tests_for(job)
        if run.cancelled:
            return {"test_run": run, "stopped": "cancelled"}
        out: FlowState = {"test_run": run}
        found = state.get("static")
        if found is not None and found.findings:  # 위반 줄이 테스트로 실행되는지 (F8)
            covered = coverage.annotate_coverage(found.findings, run.coverage_xml, ctx.worktree)
            ctx.static = out["static"] = replace(found, findings=covered)
        out["diff_coverage"] = (
            coverage.diff_coverage(run.coverage_xml, ctx.changes_diff) if ctx.changes_diff else None
        )
        return out

    def static_gate(state: FlowState) -> FixGate | None:
        found = state.get("static")
        if found is None or not found.checked or not found.findings:
            return None
        return code_fix_gate(
            state["test_run"], "static", found.findings,
            require_covered=ctx.base.repo.skills.fix.require_covered,
        )  # fmt: skip

    def history_or(job: PRJob, skill: str) -> SkillProposal:
        history, number = proposal.blocked_by_history(
            ctx.store, job.repo, job.pr_number, skill, job.head_sha
        )
        if history:
            return history, number  # 머지해 생긴 커밋, 거절된 제안, 이미 열린 제안
        return ("approval" if ctx.base.repo.skills.fix.require_approval else "auto"), None

    def skill_proposals(state: FlowState) -> dict[str, SkillProposal]:
        """스킬별 수정 제안 상태 (DESIGN.md F8 표). 위반이 없는 스킬은 빠진다."""
        job, run = state["job"], state["test_run"]
        out: dict[str, SkillProposal] = {}
        if state.get("lint") is not None:
            blocker = fix_rules.lint_fix_blocker(state["lint"], run)
            out["lint"] = (blocker, None) if blocker is not None else history_or(job, "lint")
        found = state.get("static")
        if found is not None and found.findings:
            gate = static_gate(state)
            if gate is None or not gate.allowed:
                out["static"] = ("tests", None)
            elif not gate.fixable:
                out["static"] = ("no_fixable", None)
            else:
                out["static"] = history_or(job, "static")
        return out

    def open_skills(state: FlowState, mode: str) -> list[str]:
        done = state.get("fixes", {})
        return [s for s, (p, _) in skill_proposals(state).items() if p == mode and s not in done]

    def render(state: FlowState) -> str:
        job = state["job"]
        return report.render_result(
            state["test_run"],
            job.head_sha,
            time.monotonic() - ctx.started,
            lint=state.get("lint"),
            static=state.get("static"),
            base=ctx.base,
            proposals=skill_proposals(state),
            static_gate=static_gate(state),
            diff_coverage=state.get("diff_coverage"),
            fixes=state.get("fixes"),
            proposal_results=state.get("proposals"),
            from_sync=job.source == "sync",
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
        return AWAIT_FIX if open_skills(state, "approval") else END

    def await_fix(state: FlowState) -> FlowState:
        # 상태를 체크포인트에 남기고 멈춘다. 워커는 다른 작업을 처리한다.
        # /fix <skill>이 오면 이 node부터 재개된다
        ensure_checkout(state["job"])
        skills = open_skills(state, "approval")
        approval = interrupt({"skills": skills, "head_sha": state["job"].head_sha})
        skill = approval.get("skill") if isinstance(approval, dict) else None
        return {"fix_skill": skill} if skill in skills else {}

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

    def record_call_for(job: PRJob, skill: str) -> Callable[[int, str, ClaudeResult, float], None]:
        def record(attempt: int, model: str, result: ClaudeResult, charged: float) -> None:
            ctx.store.record_ai_call(
                AICall(
                    repo=job.repo, pr_number=job.pr_number, head_sha=job.head_sha, skill=skill,
                    model=model, attempt=attempt, ok=result.ok,
                    error=str(result.error) if result.error else None, cost_usd=result.cost_usd,
                    charged_usd=charged, permission_denials=len(result.permission_denials),
                )
            )  # fmt: skip

        return record

    def ai_fix_step(state: FlowState) -> FlowState:
        """static(4단계부터 structure) 위반을 AI로 고치고 검증한다 (F7 AI 수정 루프)."""
        job, skill = state["job"], "static"
        ensure_checkout(job)
        gate = static_gate(state)
        targets = gate.fixable if gate is not None and gate.allowed else ()
        if not targets:  # 제안할 때는 있었다. 재개 사이에 바뀔 일은 없지만 방어한다
            return with_fix(state, FixOutcome(skill=skill, status="nothing_fixed"))
        fresh_worktree(job)
        progress(job, f"{skill} 수정 (AI)")
        recheck_lint = None
        if enabled("lint") and state.get("lint") is not None:

            def recheck_lint(changed):
                return lint_for(job, changed)

        tools = ai_fix.CodeFixTools(
            run_ai=lambda wt, prompt, **kw: claude_cli.run_claude(wt, prompt, **kw),
            recheck=lambda changed: static_for(job, changed),
            recheck_lint=recheck_lint,
            run_tests=lambda: tests_for(job),
            record_call=record_call_for(job, skill),
            is_stale=lambda: ctx.store.is_stale(job),
            guarded=(ctx.base.ruff_config,) if ctx.base.ruff_config else (),
        )
        outcome = ai_fix.code_fix(
            ctx.worktree, ctx.merge_base, skill, state["static"], targets,
            lint_before=state.get("lint"), test_before=state["test_run"], config=ctx.base.repo,
            spent_usd=ctx.store.ai_spent(job.repo, job.pr_number), tools=tools,
        )  # fmt: skip
        if outcome is None:
            return {"stopped": "cancelled"}
        if outcome.ready:
            outcome = seal(job, outcome)
        return with_fix(state, outcome)

    def seal(job: PRJob, outcome: FixOutcome) -> FixOutcome:
        """AI 수정안을 GitHub에 보이기 전에 diff와 AI의 설명을 비밀값 검사한다 (F10).

        걸리면 수정안 전체를 버린다. AI가 비밀값을 읽었다는 뜻이라 수정 내용도 믿을 수 없다.
        포크 PR처럼 제안 PR을 열지 않고 코멘트에만 보이는 경우도 여기서 막는다.
        """
        patch = repo.head_diff(ctx.worktree)
        secrets = proposal.pipeline_secrets()
        kind = proposal_rules.find_secret(patch, secrets) or proposal_rules.find_secret_in_text(
            outcome.explanation, secrets
        )
        if kind is not None:
            log.warning("AI 수정안을 버림 %s#%s: 비밀값 (%s)", job.repo, job.pr_number, kind)
            return replace(
                outcome, status="secret", detail=kind, explanation="", remaining=(), test_run=None
            )
        return replace(outcome, patch_path=save_patch(job, patch, outcome.skill))

    def save_patch(job: PRJob, patch: str, skill: str) -> Path:
        out = ctx.data_dir / FIX_DIR / f"{job.repo.replace('/', '__')}-pr{job.pr_number}"
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{job.head_sha[:12]}-{skill}.patch"
        path.write_text(patch)
        return path

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

    fix_targets = [AWAIT_FIX, FIX, AI_FIX, END]
    graph = StateGraph(FlowState)
    graph.add_node("checkout", checkout)
    graph.add_node("rules", rules)
    graph.add_node("test", test)
    graph.add_node("comment", comment)
    graph.add_node(AWAIT_FIX, await_fix)
    graph.add_node(FIX, fix)
    graph.add_node(AI_FIX, ai_fix_step)
    graph.add_node(PROPOSE, propose)
    graph.add_node(FIX_COMMENT, fix_comment)
    graph.add_edge(START, "checkout")
    graph.add_conditional_edges("checkout", route, {"next": "rules", "stop": END})
    graph.add_conditional_edges("rules", route, {"next": "test", "stop": END})
    graph.add_conditional_edges("test", route, {"next": "comment", "stop": END})
    graph.add_conditional_edges("comment", next_fix, fix_targets)
    graph.add_conditional_edges(AWAIT_FIX, after_await, [FIX, AI_FIX, END])
    graph.add_conditional_edges(FIX, route, {"next": PROPOSE, "stop": END})
    graph.add_conditional_edges(AI_FIX, route, {"next": PROPOSE, "stop": END})
    graph.add_conditional_edges(PROPOSE, route, {"next": FIX_COMMENT, "stop": END})
    graph.add_conditional_edges(FIX_COMMENT, next_fix, fix_targets)
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


PendingFix = Literal["awaiting", "interrupted_fix", "interrupted_after_fix", "rerun"]


def _awaiting_skills(value: object) -> list[str]:
    if not isinstance(value, dict):
        return []
    if "skills" in value:
        return list(value["skills"])
    return [value["skill"]] if "skill" in value else []  # 3단계 전 체크포인트 ({"skill": "lint"})


def pending_fix(job: PRJob, ctx: FlowContext, saver: SqliteSaver, skill: str) -> PendingFix | None:
    """이 커밋에 skill의 재개할 수정이 있는지. 승인 대기 중이거나, 수정·제안·수정 코멘트 도중
    실패해 다시 시도할 수 있다."""
    snapshot = build_graph(ctx, saver).get_state(thread_config(job))
    if AWAIT_FIX in snapshot.next and snapshot.interrupts:
        skills = _awaiting_skills(snapshot.interrupts[0].value)
        return "awaiting" if skill in skills else None
    if FIX_NODES.get(skill) in snapshot.next:
        return "interrupted_fix"
    if (PROPOSE in snapshot.next or FIX_COMMENT in snapshot.next) and snapshot.values.get(
        "fix_skill"
    ) == skill:
        return "interrupted_after_fix"
    done = snapshot.values.get("fixes", {}).get(skill)
    if not snapshot.next and done is not None and not done.ready:
        return "rerun"  # 끝난 수정이 수정안을 내지 못했다. /fix로 그 스킬만 다시 돌린다
    return None


def has_pending_fix(job: PRJob, ctx: FlowContext, skill: str) -> bool:
    with ExitStack() as stack:
        return pending_fix(job, ctx, open_checkpointer(ctx.data_dir, stack), skill) is not None


def resume_fix(job: PRJob, ctx: FlowContext, approval: dict[str, Any]) -> FlowState | None:
    """멈춰 있던 그래프를 approval["skill"]의 수정 단계부터 이어서 실행한다. 없으면 None.

    수정안을 내지 못하고 끝난 수정(실패·예산 등)도 그 스킬의 수정 단계부터 다시 돌린다.
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
        elif pending == "rerun":
            # 끝난 그래프의 그 스킬 결과만 지우고 대기 node 다음(그 스킬의 수정 node)부터 다시 간다.
            # 다른 스킬의 결과와 제안 PR은 그대로 두므로 수정 코멘트에 함께 남는다
            fixes = {k: v for k, v in graph.get_state(config).values["fixes"].items() if k != skill}
            config = graph.update_state(
                config, {"fixes": fixes, "fix_skill": skill}, as_node=AWAIT_FIX
            )
        command = Command(resume=approval) if pending == "awaiting" else None
        result = graph.invoke(command, config)
    if skill not in result.get("fixes", {}):
        result.setdefault("stopped", "stale")
    return result
