"""PR 단위 확인 (docs/DESIGN.md 4.5 끝, 완료 기준 4-11–4-14, 위협 T27).

확인 대상이 `pr`인 규칙을 PR 자체(커밋 메시지, PR 제목·본문, 브랜치 이름, 바뀐 파일)에 대해 본다.
그래프의 `pr_check` node가 모델 판단(`judge`) 다음, 테스트 전에 부른다. 가드레일(8.6)이 작업을 들인
뒤에만 돌고, `pr` 규칙이 없으면 PR의 글을 읽지 않는다.

1. 사실 읽기 (`gather`): 커밋은 이 작업이 받은 git 객체(merge-base..head)에서, 제목·본문·브랜치
   이름은 GitHub의 PR 조회에서, 바뀐 파일은 worktree의 diff에서 읽는다. 크기 상한은
   app/worker/rules/pr_check_pure.py에 있다.
2. 계획 (`plan`): 규칙마다 PR 패턴(결정적) 또는 모델 판단을 정한다. 모델은 규칙 원문과 문서의
   예시만 보고 PR의 글은 보지 않는다. 예시 확인은 샌드박스의 고정 스크립트가 한다. 계획은 규칙
   묶음 상한에서 세고 `model_cache`에 저장한다.
3. 확인 (`check`): 패턴과 바뀐 파일 조건은 샌드박스에서 맞추고, 모델 판단은 도구 없이 부른다
   (PR 상한). 판단의 답은 결정적으로 확인하고 (규칙, 사실의 해시)로 저장한다.

결과(`PrChecks`)는 알리기만 한다. 수정 판단과 수정 작성은 이 값을 받지 않는다 (4-13).
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal, Protocol

from app.worker import repo
from app.worker.budget import CallBudget
from app.worker.call_budget_pure import CALL_BUDGET_USD, Denied, PrScope, RulesetScope
from app.worker.doc_rules_pure import Found, RuleUnchecked
from app.worker.github import GitHub, GitHubError
from app.worker.repo import GitError
from app.worker.rule_pure import Rule
from app.worker.rules.pr_check_pure import (
    JUDGE_PROMPT_VERSION,
    JUDGE_SCHEMA,
    JUDGE_SYSTEM_PROMPT,
    MAX_COMMITS,
    MAX_MESSAGE_BYTES,
    PATTERN_ATTEMPTS,
    PLAN_PROMPT_VERSION,
    PLAN_SCHEMA,
    PLAN_SYSTEM_PROMPT,
    TEXT_FACTS,
    CaseResult,
    PlanDraft,
    PrChecks,
    PrFacts,
    PrPattern,
    PrPlan,
    PrRuleResult,
    PrUsage,
    PrViolation,
    Shown,
    build_judge_prompt,
    build_plan_prompt,
    candidates_shown,
    confirmed,
    example_cases,
    example_problems,
    facts_from,
    gate_ok,
    judge_key,
    parse_judge,
    parse_plan,
    partial_note,
    pattern_violations,
    pipeline_commits,
    plan_from_json,
    plan_key,
    plan_to_json,
    read_plan,
    read_results,
    real_case,
    shown_for,
    verify_judge,
    violations_from_json,
    violations_to_json,
)
from app.worker.rules.rules import AskModel
from app.worker.sandbox.checker_runner import Failed, PrRan
from app.worker.sandbox.toolcache import ToolUnavailable
from app.worker.state import Store

log = logging.getLogger(__name__)


class MatchesPrFacts(Protocol):
    def pr_pattern(
        self, decl: Mapping[str, object], cases: Sequence[Mapping[str, object]]
    ) -> PrRan | Failed: ...


@dataclass(frozen=True)
class Tools:
    store: Store
    budget: CallBudget
    ask: AskModel
    sandbox: MatchesPrFacts
    plan_model: str  # `model.checker` (검사기 작성과 같은 몫)
    judge_model: str  # `model.judge`
    ruleset: RulesetScope  # 계획 작성을 세는 규칙 묶음 상한


def pr_rules(found: object) -> tuple[Rule, ...]:
    """확인 대상이 `pr`인 규칙 (검사 방법 정하기가 검사기 없이 넘긴 것)."""
    if not isinstance(found, Found):
        return ()
    return tuple(o.rule for o in found.outcomes if isinstance(o, RuleUnchecked) and o.kind == "pr")


def gather(
    worktree: Path, merge_base: str, head_sha: str, *, github: GitHub, store: Store, repo_name: str,
    pr_number: int,
) -> PrFacts | str:  # fmt: skip
    """이 PR의 사실. 읽지 못하면 그 이유(그때 모든 PR 규칙은 "확인하지 못함"이다)."""
    try:
        text = github.pr_text(repo_name, pr_number)
        raw, more = repo.pr_commits(
            worktree, merge_base, head_sha, limit=MAX_COMMITS, message_bytes=MAX_MESSAGE_BYTES
        )
        paths = repo.changed_paths(worktree, merge_base, head_sha)
    except (GitHubError, GitError, UnicodeDecodeError) as e:
        log.warning("PR 사실을 읽지 못함: %s", e)
        return f"PR 사실을 읽지 못함: {str(e).strip().splitlines()[0] if str(e).strip() else e}"
    merged = [
        p.merge_commit for p in store.proposals(repo_name, pr_number)
        if p.state == "merged" and p.merge_commit
    ]  # fmt: skip
    return facts_from(
        title=text.title, body=text.body, branch=text.branch, commits=raw, more_commits=more,
        paths=paths, pipeline_shas=pipeline_commits(merged, raw),
    )  # fmt: skip


@dataclass
class _Run:
    plan_calls: int = 0
    judge_calls: int = 0
    cached: int = 0
    dropped: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    models: list[str] = field(default_factory=list)
    judge_stop: str = ""  # PR 상한에 걸리면 남은 판단은 묻지 않는다

    def usage(self) -> PrUsage:
        return PrUsage(
            plan_calls=self.plan_calls, judge_calls=self.judge_calls, cached=self.cached,
            dropped=self.dropped, input_tokens=self.input_tokens,
            output_tokens=self.output_tokens, models=tuple(sorted(set(self.models))),
        )  # fmt: skip


@dataclass(frozen=True)
class _NoPlan:
    """계획을 정하지 못했고 저장하지 않는다 (다음 검사가 다시 한다)."""

    kind: Literal["cap", "model_failed", "tool_unavailable"]
    detail: str


def check(
    rules: Sequence[Rule],
    facts: PrFacts | str,
    tools: Tools,
    *,
    known: int = 0,
    is_stale: Callable[[], bool] = lambda: False,
) -> PrChecks:
    """PR 규칙마다 계획을 정하고 이 PR의 사실에 맞춘다. known은 이 검사의 코드 위반 수다(PR 상한의
    대상 위반 수에 쓴다. PR 단위 위반은 세지 않는다)."""
    run = _Run()
    if isinstance(facts, str):
        results = tuple(PrRuleResult(r, "unchecked", kind="no_facts", detail=facts) for r in rules)
        return PrChecks(results, (), note=facts)
    results: list[PrRuleResult] = []
    violations: list[PrViolation] = []
    for rule in rules:
        plan = _plan(rule, tools, run)
        if isinstance(plan, _NoPlan):
            results.append(PrRuleResult(rule, "unchecked", kind=plan.kind, detail=plan.detail))
            continue
        result, found = _check_rule(rule, plan, facts, tools, run, known=known, is_stale=is_stale)
        results.append(result)
        violations += found
    commits = facts.commits
    return PrChecks(
        tuple(results), tuple(violations), commits=sum(1 for c in commits if c.checked),
        skipped_merge=sum(1 for c in commits if c.merge),
        skipped_pipeline=sum(1 for c in commits if c.pipeline and not c.merge),
        usage=run.usage(),
    )  # fmt: skip


def _plan(rule: Rule, tools: Tools, run: _Run) -> PrPlan | _NoPlan:
    """저장한 계획을 쓰거나 새로 쓴다. 예시 확인을 두 번 통과하지 못한 패턴은 글 사실이고 바뀐
    파일 조건이 예시에 맞으면 모델 판단으로, 아니면 "확인 방법 없음"으로 저장한다."""
    key = plan_key(rule, tools.plan_model)
    entry = tools.store.cached_result(key)
    if entry is not None:
        try:
            plan = plan_from_json(entry.result_json)
        except (ValueError, KeyError, TypeError):
            log.warning("저장한 PR 계획을 읽지 못해 다시 씀: %s", rule.id)
        else:
            run.cached += 1
            tools.budget.record_hit(tools.ruleset, stage="pr_plan", tier="light",
                                    model=tools.plan_model)  # fmt: skip
            return plan
    feedback: list[str] = []
    last: tuple[PlanDraft, bool] | None = None
    for attempt in range(1, PATTERN_ATTEMPTS + 1):
        call = tools.budget.begin(tools.ruleset, stage="pr_plan", tier="light",
                                  model=tools.plan_model, attempt=attempt)  # fmt: skip
        if isinstance(call, Denied):
            return _NoPlan("cap", call.reason)
        answer = tools.ask(
            build_plan_prompt(rule, feedback), system_prompt=PLAN_SYSTEM_PROMPT,
            schema=PLAN_SCHEMA, model=tools.plan_model, max_budget_usd=CALL_BUDGET_USD["light"],
        )  # fmt: skip
        failure = None if answer.ok else f"{answer.error}: {answer.detail}"
        draft = None
        if failure is None:
            try:
                draft = read_plan(parse_plan(answer.data or {}), rule)
            except ValueError as e:
                feedback = [f"답의 형식이 틀림: {str(e).splitlines()[0]}"]
        tools.budget.finish(
            call, ok=failure is None, error=failure, model_id=answer.model_id,
            input_tokens=answer.input_tokens, output_tokens=answer.output_tokens,
            permission_denials=answer.permission_denials,
        )  # fmt: skip
        run.plan_calls += 1
        run.input_tokens += answer.input_tokens or 0
        run.output_tokens += answer.output_tokens or 0
        model_id = answer.model_id or tools.plan_model
        run.models.append(model_id)
        if failure is not None:
            return _NoPlan("model_failed", failure)
        if draft is None:
            continue
        if draft.method == "none":
            return _save(key, PrPlan("none", reason=draft.reason or "PR 사실로 확인할 수 없음",
                                     model=model_id), tools)  # fmt: skip
        if draft.problems:
            feedback = list(draft.problems)
            continue
        judged = draft.method == "model"
        tried = _try_examples(draft, tools, judged=judged)
        if isinstance(tried, _NoPlan):
            return tried
        problems, gate_passed = tried
        if not problems:
            return _save(key, PrPlan(draft.method, draft.decl, draft.examples, draft.reason,
                                     "model_said" if judged else "", model_id), tools)  # fmt: skip
        feedback, last = problems, (draft, gate_passed and not judged)
    if last is not None:
        draft, gate_passed = last
        if draft.decl.fact in TEXT_FACTS and gate_passed:
            decl = PrPattern(draft.decl.fact, draft.decl.condition, "", draft.decl.when_changed)
            reason = f"PR 패턴이 예시 확인을 {PATTERN_ATTEMPTS}번 통과하지 못함: {feedback[0]}"
            return _save(key, PrPlan("model", decl, draft.examples, reason, "examples",
                                     run.models[-1]), tools)  # fmt: skip
    why = feedback[0] if feedback else "답이 없음"
    return _save(key, PrPlan("none", reason=f"예시를 통과하는 PR 패턴을 만들지 못함: {why}",
                             model=run.models[-1] if run.models else ""), tools)  # fmt: skip


def _try_examples(
    draft: PlanDraft, tools: Tools, *, judged: bool
) -> tuple[list[str], bool] | _NoPlan:
    """선언을 예시에 맞춘다. (문제 목록, 바뀐 파일 조건이 예시에 맞는지)."""
    cases = example_cases(draft.decl, draft.examples)
    ran = _run_pattern(draft.decl, cases, tools)
    if isinstance(ran, _NoPlan):
        return ran
    if isinstance(ran, str):
        return [ran], False
    return (example_problems(draft.decl, draft.examples, ran, judged=judged),
            gate_ok(draft.examples, ran, draft.decl))  # fmt: skip


def _run_pattern(
    decl: PrPattern, cases: Sequence[Mapping[str, object]], tools: Tools
) -> tuple[CaseResult, ...] | str | _NoPlan:
    """샌드박스에서 맞춘 결과, 실패 이유(str), 또는 도구를 준비하지 못함(_NoPlan)."""
    try:
        ran = tools.sandbox.pr_pattern(decl.script_input(), cases)
    except ToolUnavailable as e:
        return _NoPlan("tool_unavailable", str(e))
    if isinstance(ran, Failed):
        return ran.reason
    try:
        return read_results(ran.items, cases)
    except ValueError as e:
        return f"샌드박스 출력을 읽지 못함: {e}"


def _save(key: str, plan: PrPlan, tools: Tools) -> PrPlan:
    tools.store.save_result(
        key, stage="pr_plan", model=plan.model or tools.plan_model,
        prompt_version=PLAN_PROMPT_VERSION, result_json=plan_to_json(plan),
    )  # fmt: skip
    return plan


def _check_rule(
    rule: Rule, plan: PrPlan, facts: PrFacts, tools: Tools, run: _Run, *, known: int,
    is_stale: Callable[[], bool],
) -> tuple[PrRuleResult, tuple[PrViolation, ...]]:  # fmt: skip
    base = PrRuleResult(rule, "checked", plan.method, plan.decl, plan.route, plan.reason)
    if plan.method == "none" or plan.decl is None:
        return PrRuleResult(rule, "unchecked", "none", reason=plan.reason, kind="plan_none",
                            detail=plan.reason), ()  # fmt: skip
    decl = plan.decl
    partial = partial_note(decl.fact, bool(decl.when_changed), facts)
    if plan.method == "pattern" or decl.when_changed:
        case = real_case(decl, facts)
        ran = _run_pattern(decl, [case], tools)
        if isinstance(ran, _NoPlan):
            return PrRuleResult(rule, "unchecked", plan.method, decl, kind="tool_unavailable",
                                detail=ran.detail), ()  # fmt: skip
        if isinstance(ran, str):
            return PrRuleResult(rule, "unchecked", plan.method, decl, kind="run_failed",
                                detail=ran), ()  # fmt: skip
        (result,) = ran
        if not result.gated:
            return _with(base, status="not_applicable"), ()
        if plan.method == "pattern":
            candidates = pattern_violations(rule, decl, facts, result)
            if not candidates or decl.fact not in TEXT_FACTS:
                return _with(base, partial=partial), candidates
            # 패턴이 잡은 것은 후보다. 규칙 원문과 그 글을 모델에게 보여 위반인지 정한다 (4.5 끝)
            shown = candidates_shown(facts, candidates)
            judged, answered = _judge(rule, plan, decl, facts, tools, run, base, partial, known,
                                      is_stale, shown=shown)  # fmt: skip
            if judged.status != "checked":
                return judged, ()
            kept, dropped = confirmed(candidates, answered)
            return _with(judged, dismissed=dropped), kept
    return _judge(rule, plan, decl, facts, tools, run, base, partial, known, is_stale)


def _with(result: PrRuleResult, **changes: object) -> PrRuleResult:
    return replace(result, **changes)  # type: ignore[arg-type]


def _judge(
    rule: Rule, plan: PrPlan, decl: PrPattern, facts: PrFacts, tools: Tools, run: _Run,
    base: PrRuleResult, partial: str, known: int, is_stale: Callable[[], bool],
    shown: Shown | None = None,
) -> tuple[PrRuleResult, tuple[PrViolation, ...]]:  # fmt: skip
    """글 규칙 하나를 모델에 묻는다. 저장한 답이 있으면 묻지 않는다. shown이 있으면 그것만
    보인다(PR 패턴의 후보)."""
    shown = shown or shown_for(decl.fact, facts)
    if not shown.commits and shown.title is None:
        return _with(base, partial=partial), ()  # 볼 커밋이 없다
    key = judge_key(rule, shown, tools.judge_model)
    entry = tools.store.cached_result(key)
    if entry is not None:
        verified = violations_from_json(rule, shown, entry.result_json)
        run.cached += 1
        tools.budget.record_hit(PrScope(), stage="pr_judge", tier="light", model=tools.judge_model)
        return _with(base, partial=partial), verified.kept
    if not run.judge_stop and is_stale():
        run.judge_stop = "새 커밋이 와서 멈춤"
    if run.judge_stop:
        return _with(base, status="left", kind="cap", detail=run.judge_stop), ()
    call = tools.budget.with_targets(known).begin(
        PrScope(), stage="pr_judge", tier="light", model=tools.judge_model
    )
    if isinstance(call, Denied):
        run.judge_stop = call.reason
        return _with(base, status="left", kind="cap", detail=call.reason), ()
    answer = tools.ask(
        build_judge_prompt(rule, shown), system_prompt=JUDGE_SYSTEM_PROMPT, schema=JUDGE_SCHEMA,
        model=tools.judge_model, max_budget_usd=CALL_BUDGET_USD["light"],
    )  # fmt: skip
    failure = None if answer.ok else f"{answer.error}: {answer.detail}"
    items = ()
    if failure is None:
        try:
            items = parse_judge(answer.data or {})
        except ValueError as e:
            failure = f"답의 형식이 틀림: {str(e).splitlines()[0]}"
    tools.budget.finish(
        call, ok=failure is None, error=failure, model_id=answer.model_id,
        input_tokens=answer.input_tokens, output_tokens=answer.output_tokens,
        permission_denials=answer.permission_denials,
    )  # fmt: skip
    run.judge_calls += 1
    run.input_tokens += answer.input_tokens or 0
    run.output_tokens += answer.output_tokens or 0
    run.models.append(answer.model_id or tools.judge_model)
    if failure is not None:
        log.warning("PR 단위 모델 판단 실패: %s", failure)
        return _with(base, status="left", kind="model_failed", detail=failure), ()
    verified = verify_judge(rule, shown, items)
    for reason in verified.dropped:
        log.info("PR 단위 판단의 답을 버림: %s", reason)
    run.dropped += len(verified.dropped)
    tools.store.save_result(key, stage="pr_judge", model=answer.model_id or tools.judge_model,
                            prompt_version=JUDGE_PROMPT_VERSION,
                            result_json=violations_to_json(verified.kept))  # fmt: skip
    return _with(base, partial=partial), verified.kept
