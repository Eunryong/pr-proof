"""문서 규칙 경로 (docs/DESIGN.md 5.1, 13절 1단계): 규칙 읽기 → 검사기 작성 → 위반 찾기.

v1의 lint·static과 따로 돌고, 찾기만 하고 고치지 않는다. 이 경로의 실패(모델 호출 실패, 호출
상한, 도구 준비 실패, 샌드박스 오류)는 작업을 실패시키지 않고 `NotRun`이나 규칙별 "검사하지
못함"으로 남는다. 테스트와 v1 결과는 그대로 나온다.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from app.models.call_budget import RulesetScope, limits_for
from app.models.checker import Checker
from app.models.checker_authoring import (
    Checked,
    RuleCheck,
    Unchecked,
    checker_from_json,
    checker_key,
)
from app.models.diff import added_lines
from app.models.doc_rules import (
    CheckerRef,
    DocRules,
    Found,
    NoRuleDocs,
    NoRulesFound,
    NotRun,
    RuleChecked,
    RuleOutcome,
    RulesRead,
    RuleUnchecked,
    call_usage,
    with_run_failures,
)
from app.models.repo_config import ModelSettings, RulesSettings
from app.sandbox.docker_cli import SandboxError
from app.worker import checkers, detect, rules
from app.worker.budget import CallBudget
from app.worker.checkers import RunsCheckers
from app.worker.repo import GitError
from app.worker.state import Store

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Runtime:
    store: Store
    budget: CallBudget
    models: ModelSettings
    ask: rules.AskModel
    sandbox: RunsCheckers


def read(
    worktree: Path, base_sha: str, settings: RulesSettings, rt: Runtime
) -> RulesRead | DocRules:
    """base의 규칙 문서를 읽는다. 검사할 규칙이 있으면 RulesRead, 없거나 못 읽었으면 끝난 결과."""
    try:
        result = rules.load_rules(
            worktree, base_sha, settings, model=rt.models.light, store=rt.store,
            budget=rt.budget, ask=rt.ask,
        )  # fmt: skip
    except GitError as e:
        log.warning("규칙 문서를 읽지 못함: %s", e)
        return NotRun(f"base의 규칙 문서를 읽지 못함: {_first_line(str(e))}")
    match result:
        case rules.RulesFound(key=key, ruleset=ruleset, rules=found):
            return RulesRead(key, found, disabled=len(ruleset.rules) - len(found))
        case rules.NoRuleDocuments(unread=unread):
            return NoRuleDocs(unread)
        case rules.NoRules(ruleset=ruleset):
            return NoRulesFound(dropped=len(ruleset.dropped))
        case rules.ModelFailed(reason=reason):
            return NotRun(f"규칙 읽기의 모델 호출이 실패함: {_first_line(reason)}")
        case rules.CapReached(denied=denied):
            return NotRun(f"규칙을 읽지 않음: {denied.reason}")


def author(read: RulesRead, rt: Runtime) -> tuple[RuleOutcome, ...] | NotRun:
    """규칙마다 검사기를 작성하거나 저장한 것을 찾는다."""
    try:
        checks = checkers.plan_and_author(
            read.rules, ruleset_key=read.key, store=rt.store, budget=rt.budget,
            models=rt.models, ask=rt.ask, sandbox=rt.sandbox,
        )  # fmt: skip
    except SandboxError as e:
        log.warning("검사기 확인 샌드박스 실패: %s", e)
        return NotRun(f"검사기를 확인하는 샌드박스가 실패함: {_first_line(str(e))}")
    return tuple(_outcome(c, rt.models) for c in checks)


def _outcome(check: RuleCheck, models: ModelSettings) -> RuleOutcome:
    if isinstance(check, Unchecked):
        return RuleUnchecked(check.rule, check.kind, check.detail)
    key = checker_key(check.rule, models.light, models.heavy)
    ref = CheckerRef(key=key, id=check.checker.id, kind=check.checker.kind, cached=check.cached)
    return RuleChecked(check.rule, ref)


def find(
    read: RulesRead,
    outcomes: tuple[RuleOutcome, ...],
    worktree: Path,
    changes_diff: bytes,
    rt: Runtime,
    *,
    since_call: int,
) -> Found | NotRun:
    """채택한 검사기를 PR이 바꾼 파일에 돌려 바뀐 줄의 위반만 남긴다.

    since_call은 이 검사를 시작하기 전 마지막 호출 기록의 id다. 그 뒤의 기록만 호출 수에 센다.
    """
    checks = [_saved_check(o, rt.store) for o in outcomes]
    changed = added_lines(changes_diff.decode("utf-8", errors="replace"))
    try:
        found = detect.detect(checks, worktree, changed, sandbox=rt.sandbox)
    except SandboxError as e:
        log.warning("검사기 실행 샌드박스 실패: %s", e)
        return NotRun(f"검사기를 돌리는 샌드박스가 실패함: {_first_line(str(e))}")
    failed = [RuleUnchecked(u.rule, u.kind, u.detail) for u in found.unchecked]
    limit = limits_for(rt.budget.settings, RulesetScope(read.key, len(read.rules))).scope
    calls = [
        c for c in rt.store.model_calls(rt.budget.repo, rt.budget.pr_number)
        if (c.id or 0) > since_call and c.head_sha == rt.budget.head_sha
    ]  # fmt: skip
    return Found(
        outcomes=with_run_failures(outcomes, failed),
        violations=found.violations,
        disabled=read.disabled,
        usage=call_usage(calls, limit),
    )


def _saved_check(outcome: RuleOutcome, store: Store) -> RuleCheck:
    if isinstance(outcome, RuleUnchecked):
        return Unchecked(outcome.rule, outcome.kind, outcome.detail)
    entry = store.saved_checker(outcome.checker.key)
    if entry is None or entry.checker_json is None:
        return Unchecked(outcome.rule, "run_failed", "저장한 검사기를 찾지 못함")
    return Checked(outcome.rule, checker_from_json(entry.checker_json), outcome.checker.cached)


def shown_checkers(result: DocRules | None, store: Store) -> dict[str, Checker]:
    """코멘트에 본문을 보일 검사기: 이번 검사에서 위반을 찾은 것 (4.9). {CheckerRef.key: 검사기}."""
    if not isinstance(result, Found):
        return {}
    found_by = {v.rule_id for v in result.violations}
    out = {}
    for o in result.outcomes:
        if isinstance(o, RuleChecked) and o.rule.id in found_by:
            entry = store.saved_checker(o.checker.key)
            if entry is not None and entry.checker_json is not None:
                out[o.checker.key] = checker_from_json(entry.checker_json)
    return out


def last_call_id(store: Store, repo: str, pr_number: int) -> int:
    return max((c.id or 0 for c in store.model_calls(repo, pr_number)), default=0)


def sandbox_for(tool_cache: Path) -> RunsCheckers:
    return checkers.Sandbox(tool_cache)


def _first_line(text: str) -> str:
    return text.strip().splitlines()[0] if text.strip() else text
