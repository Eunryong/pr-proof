"""작업 흐름 그래프의 단계별 node와 분기 함수 (docs/DESIGN.md 5.1, 5.2).

각 함수는 `FlowContext`와 `FlowState`를 받아 다음 상태 갱신(또는 분기 이름)을 돌려준다.
`app/worker/graph.py`의 `build_graph`가 이 함수들을 묶어 LangGraph로 조립한다.
"""

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace

from langgraph.graph import END
from langgraph.types import interrupt

from app.shared.job_pure import PRJob
from app.worker import records
from app.worker.config.access_pure import is_trusted
from app.worker.env import default_limits, use
from app.worker.env.plan import plan_environment
from app.worker.env.use_pure import EnvRef
from app.worker.fix import (
    decide,
    mechanical_fix,
    model_fix,
    model_fix_pure,
    proposal,
)
from app.worker.fix.fix_decision_pure import SafetyNet, open_targets
from app.worker.fix.model_fix_pure import Picked
from app.worker.fix.proposal_pure import ProposalResult
from app.worker.flow_state import (
    AWAIT_FIX,
    BUNDLES,
    FIX_NODES,
    MECHANICAL,
    MODEL,
    STATE_KEYS,
    FlowContext,
    FlowState,
    Route,
    doc_runtime,
    ensure_checkout,
    enter_checkout,
    fresh_worktree,
    keep_alive,
    progress,
    rag_switch,
    uses_plan,
)
from app.worker.lang import python
from app.worker.model.model_backend import fix_unavailable
from app.worker.report import report
from app.worker.report.doc_rules_report import FixView
from app.worker.rules import (
    detect,
    doc_rules,
    doc_rules_pure,
    linked_issues,
    linked_issues_pure,
    pr_check,
    pr_description,
    pr_description_pure,
    run_check,
    run_check_pure,
)
from app.worker.rules.checker_authoring_pure import Checked
from app.worker.rules.doc_rules_pure import DocRules, Found, RulesRead
from app.worker.rules.pr_check_pure import PrFacts
from app.worker.store.call_budget_pure import RulesetScope
from app.worker.store.rag_pure import Retrieval
from app.worker.testing import coverage
from app.worker.testing.generated_tests_pure import Suite
from app.worker.testing.test_run_pure import TestRun

TestsFn = Callable[[PRJob, EnvRef | None], TestRun]
SuiteFn = Callable[[PRJob, Mapping[str, bytes] | None, Sequence[str]], Suite]


def _doc_rules_done(ctx: FlowContext, result: DocRules) -> FlowState:
    ctx.doc_rules = result  # 의존성 설치가 실패해도(그래프 밖 예외) 코멘트에 보인다
    return {"doc_rules": result}


def _pr_facts(ctx: FlowContext, job: PRJob) -> PrFacts | str:
    """이 작업의 PR 사실. 처음 부를 때 한 번 읽는다 (GitHub 조회 한 번)."""
    if ctx.pr_facts is None:
        ctx.pr_facts = pr_check.gather(
            ctx.worktree, ctx.merge_base or job.base_sha, job.head_sha, github=ctx.github,
            store=ctx.store, repo_name=job.repo, pr_number=job.pr_number,
        )  # fmt: skip
    return ctx.pr_facts


def checkout(ctx: FlowContext, state: FlowState) -> FlowState:
    enter_checkout(ctx, state["job"])
    return {}


def load_rules(ctx: FlowContext, state: FlowState) -> FlowState:
    """규칙 문서와 base의 도구 설정을 읽는다 (5.1 load_rules). 규칙이 없으면 여기서 끝난다.
    믿는 작성자의 PR이 규칙 문서를 고쳤으면 그 PR의 문서를 읽는다 (4.3)."""
    job = state["job"]
    progress(ctx, job, "규칙 읽기")
    ctx.first_call = doc_rules.last_call_id(ctx.store, job.repo, job.pr_number)
    switch = rag_switch(ctx, state)
    rt = doc_runtime(ctx, {**state, "rag_switch": switch})
    if rt.retriever is not None:  # 검색을 켠 첫 검사에서 폴더를 만들고 밀린 기록을 넣는다 (6.5)
        rt.retriever.prepare()
    read = doc_rules.read(
        ctx.worktree, job.base_sha, ctx.base.repo.rules, rt, ctx.changes_diff,
        head_sha=job.head_sha, fork_sha=ctx.merge_base,
        trusted=is_trusted(ctx.author, ctx.base.access),
    )  # fmt: skip
    if isinstance(read, RulesRead):
        return {"rules_read": read, "rag_switch": switch}
    return {**_doc_rules_done(ctx, read), "rag_switch": switch}


def author_checkers(ctx: FlowContext, state: FlowState) -> FlowState:
    """규칙마다 도구나 검사기를 정하거나 저장한 것을 쓴다 (5.1 plan_checks, author_checker)."""
    job = state["job"]
    used: list[Retrieval] = []
    outcomes = doc_rules.author(
        state["rules_read"], doc_runtime(ctx, state), is_stale=lambda: ctx.store.is_stale(job),
        on_retrieval=used.append,
    )  # fmt: skip
    if isinstance(outcomes, tuple):
        return {"rule_checks": outcomes, "checker_retrievals": tuple(used)}
    return _doc_rules_done(ctx, outcomes)


def detect_rules(ctx: FlowContext, state: FlowState) -> FlowState:
    """채택한 도구와 검사기를 PR이 바꾼 파일에 돌린다 (5.1 detect). 바뀐 줄의 위반만 남긴다."""
    result = doc_rules.find(
        state["rules_read"], state["rule_checks"], ctx.worktree, ctx.changes_diff,
        doc_runtime(ctx, state), since_call=ctx.first_call,
        retrievals=state.get("checker_retrievals", ()),
    )  # fmt: skip
    return _doc_rules_done(ctx, result)


def judge_rules(ctx: FlowContext, state: FlowState) -> FlowState:
    """검사기를 만들지 못한 규칙을 모델이 바뀐 hunk에 대해 본다 (5.1 detect의 모델 판단, 4.5).

    그 뒤 사람이 닫은 수정안이 고친 줄에 표시하고(4.6 표의 첫 줄), 작성자가 고친 이전 위반과
    이번 위반을 기록한다 (4.10). `/fix`가 남은 모델 판단을 이어 할 때도 이 node부터 돈다.
    """
    job, result = state["job"], state.get("doc_rules")
    if not isinstance(result, Found):
        return {}
    ensure_checkout(ctx, job)  # `/fix`가 이어 할 때는 새 프로세스다
    if any(isinstance(o, doc_rules_pure.RuleJudged) for o in result.outcomes):
        progress(ctx, job, "모델 판단")
        result = doc_rules.judge(
            result, ctx.worktree, ctx.changes_diff, doc_runtime(ctx, state),
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
    return _doc_rules_done(ctx, result)


def pr_check_rules(ctx: FlowContext, state: FlowState) -> FlowState:
    """확인 대상이 `pr`인 규칙을 PR 자체에 대해 본다 (4.5 끝). PR 규칙이 없으면 PR의 글을 읽지
    않는다. 가드레일(8.6)이 들인 작업에서만 이 node까지 온다. `/fix`가 남은 PR 단위 판단을 이어
    할 때도 이 node를 지난다."""
    job, found = state["job"], state.get("doc_rules")
    rules_ = pr_check.pr_rules(found)
    if not rules_:
        return {}
    ensure_checkout(ctx, job)
    progress(ctx, job, "PR 단위 확인")
    rt = doc_runtime(ctx, state)
    read = state.get("rules_read")
    scope = RulesetScope(read.key, len(read.rules)) if read is not None else RulesetScope("pr")
    facts = _pr_facts(ctx, job)
    tools = pr_check.Tools(
        store=rt.store, budget=rt.budget, ask=rt.backend.ask, sandbox=rt.sandbox,
        plan_model=rt.models.checker, judge_model=rt.models.judge, ruleset=scope,
    )  # fmt: skip
    known = len(found.violations) if isinstance(found, Found) else 0
    result = pr_check.check(
        rules_, facts, tools, known=known, is_stale=lambda: ctx.store.is_stale(job)
    )
    ctx.pr_checks = result
    return {"pr_checks": result}


def describe_pr(ctx: FlowContext, state: FlowState) -> FlowState:
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
    ensure_checkout(ctx, job)
    progress(ctx, job, "PR 설명 확인")
    rt, found = doc_runtime(ctx, state), state.get("doc_rules")
    facts = _pr_facts(ctx, job)
    pipeline = () if isinstance(facts, str) else pr_description.pipeline_diffs(ctx.worktree, facts)
    result = pr_description.check(
        facts, ctx.changes_diff.decode("utf-8", errors="replace"), ctx.worktree,
        pr_description.Tools(
            store=rt.store, budget=rt.budget, read_code=rt.backend.reading,
            model=rt.models.judge, max_diff_chars=rt.backend.capabilities.max_diff_chars,
        ),
        pipeline=pipeline, known=len(found.violations) if isinstance(found, Found) else 0,
        is_stale=lambda: ctx.store.is_stale(job),
    )  # fmt: skip
    ctx.pr_description = result
    return {"pr_description": result}


def check_issues(ctx: FlowContext, state: FlowState) -> FlowState:
    """PR이 닫겠다고 한 이슈가 요구한 것과 diff를 모델이 견준다 (4.5 끝). 규칙과 따로 돌고,
    알리기만 한다. 가드레일(8.6)이 들인 작업에서만 이 node까지 와서 이슈를 읽는다. 연결된 이슈가
    없으면 모델을 부르지 않는다. `.pipeline.yaml`의 `linked_issues.enabled: false`면 돌지
    않는다. `/fix`가 앞 node부터 다시 돌 때 이미 견준 결과가 있으면(호출 상한으로 남은 것이
    아니면) 그대로 둔다."""
    job = state["job"]
    if not ctx.base.repo.linked_issues.enabled:
        return {}
    saved = state.get("linked_issues")
    if saved is not None and not linked_issues_pure.issues_left(saved):
        return {}
    ensure_checkout(ctx, job)
    linked = linked_issues.read(ctx.github, job.repo, job.pr_number)
    # 연결된 이슈가 없으면 진행 글을 고치지 않는다 (GitHub 호출 하나를 아낀다)
    if not isinstance(linked, str) and linked.issues:
        progress(ctx, job, "이슈 확인")
    rt, found = doc_runtime(ctx, state), state.get("doc_rules")
    result = linked_issues.check(
        linked, job.repo,
        ctx.changes_diff.decode("utf-8", errors="replace"), ctx.worktree,
        linked_issues.Tools(
            store=rt.store, budget=rt.budget, read_code=rt.backend.reading,
            model=rt.models.judge, max_diff_chars=rt.backend.capabilities.max_diff_chars,
        ),
        known=len(found.violations) if isinstance(found, Found) else 0,
        is_stale=lambda: ctx.store.is_stale(job),
    )  # fmt: skip
    ctx.linked_issues = result
    return {"linked_issues": result}


def after_doc_step(ctx: FlowContext, state: FlowState) -> str:
    if state.get("stopped") or ctx.store.is_stale(state["job"]):
        return "stop"
    return "done" if "doc_rules" in state else "next"


def plan_env(ctx: FlowContext, state: FlowState) -> FlowState:
    """Python 프로필이 돌릴 것이 없는 저장소의 환경 처방을 정하거나 저장한 것을 쓴다 (4.5).

    처방은 base에만 의존해 저장소 단위로 저장하고, 기댄 파일이 그대로면 모델도 설치도 부르지 않는다.
    새 커밋이 와도 멈추지 않는다(구운 층은 다음 커밋에도 쓴다). 낡은 커밋이면 다음 분기가 멈춘다.
    """
    job = state["job"]
    ensure_checkout(ctx, job)
    if not uses_plan(ctx.worktree):
        return {}
    progress(ctx, job, "환경 준비")
    rt = doc_runtime(ctx, state)
    with keep_alive(ctx):
        plan = plan_environment(
            ctx.worktree, job.base_sha, store=rt.store, budget=rt.budget, model=rt.models.env,
            read_code=rt.backend.reading, limits=default_limits(), names=ctx.sandbox_names,
        )  # fmt: skip
    return {"env": use.ref_of(plan)}


def test(ctx: FlowContext, state: FlowState, *, tests_for: TestsFn) -> FlowState:
    job = state["job"]
    progress(ctx, job, "테스트 실행")
    run = tests_for(job, state.get("env"))
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
        # 실행 결과의 규칙은 테스트와 정적 검사의 결과를 보고 모델이 정한다 (4.5)
        progress(ctx, job, "실행 결과의 규칙")
        runs = run_check.judge_runs(
            doc, run, doc_runtime(ctx, state),
            is_stale=lambda: ctx.store.is_stale(job),
        )  # fmt: skip
        ctx.doc_rules = out["doc_rules"] = replace(doc, runs=runs)
    return out


def decide_fixes(ctx: FlowContext, state: FlowState) -> FlowState:
    """위반마다 fix/skip을 정한다 (5.1 decide). 표가 테스트 상태와 커버리지를 본다."""
    job, found = state["job"], state.get("doc_rules")
    if not isinstance(found, Found) or not found.violations:
        return {}
    ensure_checkout(ctx, job)  # `/fix`가 다시 돌릴 때는 새 프로세스다
    progress(ctx, job, "수정 판단")
    rt, run = doc_runtime(ctx, state), state["test_run"]
    decided = decide.decide(
        found, SafetyNet.of(run), ctx.changes_diff, store=rt.store,
        budget=rt.budget, model=rt.models.decide, ask=rt.backend.ask, retriever=rt.retriever,
        context_of=records.context_of(ctx.worktree),
        manual=bool(state.get("model_requested")), current_pr=job.pr_number,
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


def fix_view(ctx: FlowContext, state: FlowState, bundle: str) -> FixView | None:
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


def awaiting(ctx: FlowContext, state: FlowState) -> list[str]:
    """승인을 기다리는 묶음."""
    return [
        b for b in BUNDLES if (v := fix_view(ctx, state, b)) is not None and v.state == "approval"
    ]


def render(ctx: FlowContext, state: FlowState) -> str:
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
        fixes={b: v for b in BUNDLES if (v := fix_view(ctx, state, b)) is not None},
        rag=state.get("rag_switch"),
        pr=state.get("pr_checks"),
        carried=state.get("carried"),
        description=state.get("pr_description"),
        issues=state.get("linked_issues"),
        env=use.view(ctx.store, state["test_run"].env),
    )


def comment(ctx: FlowContext, state: FlowState) -> FlowState:
    job = state["job"]
    ctx.comment_id = ctx.github.upsert_comment(
        job.repo, job.pr_number, render(ctx, state), ctx.comment_id, head_sha=job.head_sha
    )
    return {"commented": True}


def next_fix(ctx: FlowContext, state: FlowState) -> str:
    """코멘트 뒤: 바로 고칠 묶음이 있으면 그 수정, 승인을 기다릴 묶음이 있으면 대기."""
    if ctx.store.is_stale(state["job"]):
        return END
    for bundle in BUNDLES:
        view = fix_view(ctx, state, bundle)
        if view is not None and view.state == "running":
            return FIX_NODES[bundle]
    return AWAIT_FIX if awaiting(ctx, state) else END


def await_fix(ctx: FlowContext, state: FlowState) -> FlowState:
    # 상태를 체크포인트에 남기고 멈춘다. 워커는 다른 작업을 처리한다.
    # /fix <묶음>이 오면 이 node부터 재개된다
    ensure_checkout(ctx, state["job"])
    bundles = awaiting(ctx, state)
    approval = interrupt({"skills": bundles, "head_sha": state["job"].head_sha})
    bundle = approval.get("skill") if isinstance(approval, dict) else None
    if bundle not in bundles:
        return {"fix_skill": ""}
    return {"fix_skill": bundle, STATE_KEYS[bundle][1]: True}


def after_await(state: FlowState) -> str:
    return FIX_NODES.get(state.get("fix_skill") or "", END)


def fix_mechanical(ctx: FlowContext, state: FlowState, *, tests_for: TestsFn) -> FlowState:
    """fix·mechanical로 정한 위반을 도구의 수정으로 고친다 (5.1 fix_mechanical, 4.7, 4.8)."""
    job, found = state["job"], state["doc_rules"]
    picked = targets(state, MECHANICAL)
    fresh_worktree(ctx, job)
    progress(ctx, job, "기계적 수정")
    rt = doc_runtime(ctx, state)
    notes = model_fix.notes_for(found, picked, ctx.worktree)
    outcome = mechanical_fix.fix_violations(
        found, picked, worktree=ctx.worktree, merge_base=ctx.merge_base,
        base_sha=job.base_sha, test_before=state["test_run"],
        checks=doc_rules.saved_checks(found, rt.store), sandbox=rt.sandbox,
        run_tests=lambda: tests_for(job, None), is_stale=lambda: ctx.store.is_stale(job),
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


def deliver_mechanical(ctx: FlowContext, state: FlowState) -> FlowState:
    """검증을 통과한 기계적 수정안만 수정안 PR로 올린다 (5.1 deliver). 늘 새 worktree에 저장한
    패치를 적용하고 파일 해시를 맞춰 본 뒤 올린다 (4.8 끝)."""
    fix = state["mechanical_fix"]
    if not fix.ready:
        return {}
    job = state["job"]
    fresh_worktree(ctx, job)
    progress(ctx, job, "수정안 PR (기계적 수정)")
    stored = model_fix.reapply(ctx.store, fix.attempt_id, ctx.worktree)
    if isinstance(stored, str):
        result = ProposalResult(reason=stored)
    else:
        result = proposal.open_mechanical_proposal(
            job, ctx.worktree, ctx.store, ctx.github, fix, state["doc_rules"],
            patch=stored.patch or "", tool=ctx.base.ruff.label,
        )  # fmt: skip
    return {"proposals": {**state.get("proposals", {}), MECHANICAL: result}}


def fix_model(ctx: FlowContext, state: FlowState, *, suite_for: SuiteFn) -> FlowState:
    """fix·model로 정한 위반을 모델이 도구로 확인하며 고치고 출구 검사를 한다 (5.1)."""
    job, found = state["job"], state["doc_rules"]
    picked = targets(state, MODEL)
    fresh_worktree(ctx, job)
    progress(ctx, job, "모델 수정")
    rt = doc_runtime(ctx, state)
    budget = rt.budget.with_targets(open_targets(state["fix_decisions"].decisions))
    checks = doc_rules.saved_checks(found, rt.store)
    tools = model_fix.FixTools(
        run_model=ctx.backend.fix,
        unavailable=fix_unavailable(ctx.backend),
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
    outcome = model_fix.save_attempt(ctx.store, job, ctx.merge_base, outcome, notes, ctx.worktree)
    records.after_failed_fix(
        ctx.store, job, outcome, notes, ctx.worktree, proposal.pipeline_secrets()
    )
    return {"model_fix": outcome}


def deliver_model(ctx: FlowContext, state: FlowState) -> FlowState:
    """검증을 통과한 모델 수정안만 수정안 PR로 올린다 (5.1 deliver).

    늘 새 worktree에 저장한 패치(`fix_attempts`)를 적용하고 파일 해시를 맞춰 본 뒤 올린다
    (4.8 끝). 전달이 실패해 재시도가 이 node부터 이어 가도 모델을 다시 부르지 않고 같은 패치가
    올라간다 (3.1의 C6).
    """
    fix = state["model_fix"]
    if not fix.ready:
        return {}
    job = state["job"]
    fresh_worktree(ctx, job)
    progress(ctx, job, "수정안 PR (모델 수정)")
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


def fix_comment(ctx: FlowContext, state: FlowState) -> FlowState:
    job = state["job"]
    ctx.comment_id = ctx.github.upsert_comment(
        job.repo, job.pr_number, render(ctx, state), ctx.comment_id, head_sha=job.head_sha
    )
    return {}


def route(ctx: FlowContext, state: FlowState) -> Route:
    if state.get("stopped") or ctx.store.is_stale(state["job"]):
        return "stop"
    return "next"
