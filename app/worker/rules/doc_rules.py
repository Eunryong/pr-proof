"""규칙 경로 (docs/DESIGN.md 5.1): 규칙 읽기 → 검사 방법 정하기(도구, 검사기 작성) → 위반 찾기.

규칙은 base의 문서(모델이 읽음)와 도구 설정(ruff 설정, 결정적으로 읽음)에서 나온다. 둘 다 없으면
규칙 검사를 하지 않는다 (2.2). 찾기만 한다(수정은 app/worker/fix/mechanical_fix.py,
app/worker/fix/model_fix.py). 이 경로의 실패(모델 호출 실패, 호출 상한, 도구 준비 실패, 샌드박스
오류)는 작업을 실패시키지 않고 `NotRun`이나 규칙별 "검사하지 못함"으로 남는다. 규칙 문서를 읽지
못해도 도구 설정의 규칙은 검사한다 (4.3 "실패 시"). 테스트 결과는 그대로 나온다.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from app.worker.ai.model_backend import ModelBackend
from app.worker.budget import CallBudget
from app.worker.call_budget_pure import RulesetScope, limits_for
from app.worker.checker_authoring_pure import (
    Checked,
    RuleCheck,
    ToJudge,
    Unchecked,
    checker_from_json,
    checker_key,
)
from app.worker.config.repo_config_pure import ModelSettings, RulesSettings
from app.worker.core.checker_pure import TOOL_KINDS, Checker, tool_codes
from app.worker.core.diff_pure import added_lines, new_files
from app.worker.core.rule_pure import checked_exclude
from app.worker.core.tools_pure import RuffSetup, config_rule
from app.worker.doc_rules_pure import (
    CheckerRef,
    DocRules,
    Found,
    NoRuleDocs,
    NoRulesFound,
    NotRun,
    RuleChecked,
    RuleJudged,
    RuleOutcome,
    RulesRead,
    RuleUnchecked,
    call_usage,
    with_run_failures,
)
from app.worker.git import repo
from app.worker.git.repo import GitError
from app.worker.memory import FindsRecords
from app.worker.rag_pure import Retrieval, merge_retrievals
from app.worker.rule_reading_pure import apply_disabled
from app.worker.rules import checkers, detect, rules
from app.worker.rules import judge as model_judge
from app.worker.rules.tool_choice_pure import choice_key
from app.worker.sandbox import docker_cli
from app.worker.sandbox.docker_cli import SandboxError
from app.worker.state import Store

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Runtime:
    store: Store
    budget: CallBudget
    models: ModelSettings
    backend: ModelBackend  # 모델 구현 (`app/worker/ai/model_backend.py`)
    sandbox: checkers.FormatsFiles
    ruff: RuffSetup | None = None  # 이 저장소의 ruff (base 설정, 버전)
    disabled: frozenset[str] = frozenset()  # `rules.disabled`. 도구 설정 규칙의 ruff 코드를 끈다
    retriever: FindsRecords | None = None  # `rag.enabled`일 때만 (6.2)
    secrets: tuple[str, ...] = ()  # 기록을 저장하기 전에 찾는 파이프라인 비밀값 (6.5)


def read(
    worktree: Path, base_sha: str, settings: RulesSettings, rt: Runtime, changes_diff: bytes = b""
) -> RulesRead | DocRules:
    """base의 규칙 문서와 도구 설정을 읽는다. 검사할 규칙이 있으면 RulesRead, 없으면 끝난 결과.

    changes_diff(PR의 diff)는 규칙 문서가 400KB를 넘는 저장소에서 관련 절을 찾는 데 쓴다 (6.3).
    """
    configured = (config_rule(rt.ruff.config),) if rt.ruff and rt.ruff.config else ()
    tool_rules = apply_disabled(configured, settings.disabled)
    docs = _read_documents(worktree, base_sha, settings, rt, changes_diff)
    if isinstance(docs, RulesRead):
        return replace(docs, rules=(*docs.rules, *tool_rules),
                       disabled=docs.disabled + len(configured) - len(tool_rules))  # fmt: skip
    if not tool_rules:
        return docs
    note = docs.reason if isinstance(docs, NotRun) else ""
    return RulesRead("", tool_rules, disabled=0, note=note)


def _read_documents(
    worktree: Path, base_sha: str, settings: RulesSettings, rt: Runtime, changes_diff: bytes
) -> RulesRead | DocRules:
    """규칙 문서를 모델로 읽는다. 규칙이 있으면 RulesRead, 없거나 못 읽었으면 끝난 결과."""
    try:
        result = rules.load_rules(
            worktree, base_sha, settings, model=rt.models.rules, store=rt.store,
            budget=rt.budget, ask=rt.backend.ask, sections=rt.retriever, changes_diff=changes_diff,
        )  # fmt: skip
    except GitError as e:
        log.warning("규칙 문서를 읽지 못함: %s", e)
        return NotRun(f"base의 규칙 문서를 읽지 못함: {_first_line(str(e))}")
    match result:
        case rules.RulesFound(key=key, ruleset=ruleset, rules=found, sections=read_sections):
            if any(r.exclude for r in found):
                # 범위 추정의 효과 확인. 규칙 묶음은 저장된 것이어도 이 PR의 base 목록으로 매번 본다
                try:
                    base_files = repo.list_base_files(worktree, base_sha)
                except GitError as e:
                    log.warning("base의 파일 목록을 읽지 못함: %s", e)
                    return NotRun(f"base의 파일 목록을 읽지 못함: {_first_line(str(e))}")
                found = tuple(checked_exclude(r, base_files) for r in found)
            return RulesRead(key, found, disabled=len(ruleset.rules) - len(found),
                             sections=read_sections)  # fmt: skip
        case rules.NoRuleDocuments(unread=unread):
            return NoRuleDocs(unread)
        case rules.NoRules(ruleset=ruleset):
            return NoRulesFound(dropped=len(ruleset.dropped))
        case rules.ModelFailed(reason=reason):
            return NotRun(f"규칙 읽기의 모델 호출이 실패함: {_first_line(reason)}")
        case rules.CapReached(denied=denied):
            return NotRun(f"규칙을 읽지 않음: {denied.reason}")


def author(
    read: RulesRead,
    rt: Runtime,
    *,
    is_stale: Callable[[], bool] = lambda: False,
    on_retrieval: Callable[[Retrieval], None] = lambda r: None,
) -> tuple[RuleOutcome, ...] | NotRun:
    """규칙마다 검사기를 작성하거나 저장한 것을 찾는다. 검색으로 넣은 검사기 기록은
    on_retrieval로 알린다."""
    try:
        checks = checkers.plan_and_author(
            read.rules, ruleset_key=read.key, store=rt.store, budget=rt.budget,
            models=rt.models, ask=rt.backend.ask, sandbox=rt.sandbox, ruff=rt.ruff,
            is_stale=is_stale, retriever=rt.retriever, on_retrieval=on_retrieval,
            secrets=rt.secrets,
        )  # fmt: skip
    except SandboxError as e:
        log.warning("검사기 확인 샌드박스 실패: %s", e)
        return NotRun(f"검사기를 확인하는 샌드박스가 실패함: {_first_line(str(e))}")
    return tuple(_outcome(c, rt.models, rt.ruff) for c in checks)


def _outcome(check: RuleCheck, models: ModelSettings, ruff: RuffSetup | None) -> RuleOutcome:
    if isinstance(check, ToJudge):
        # 검사기를 채택하지 않은 내용 규칙은 모델이 hunk를 직접 본다 (4.4의 3번). 경로 규칙은 오지
        # 않는다 (app/worker/rules/checkers.py)
        return RuleJudged(check.rule, check.detail, check.route)
    if isinstance(check, Unchecked):
        return RuleUnchecked(check.rule, check.kind, check.detail)
    key = _saved_key(check, models, ruff)
    codes = tool_codes(check.checker.body) if check.checker.kind == "ruff" else ()
    ref = CheckerRef(key=key, id=check.checker.id, kind=check.checker.kind, cached=check.cached,
                     codes=codes)  # fmt: skip
    return RuleChecked(check.rule, ref)


def _saved_key(check: Checked, models: ModelSettings, ruff: RuffSetup | None) -> str:
    """채택한 확인 방법을 저장한 `checkers`의 키: 도구 설정, 도구 선택, 또는 검사기 작성."""
    if check.rule.origin == "tool_config" and ruff is not None:
        return checkers.config_key(check.rule, ruff)
    if check.checker.kind in TOOL_KINDS and ruff is not None:
        return choice_key(check.rule, models.checker, ruff.version)
    return checker_key(check.rule, models.checker)


def find(
    read: RulesRead,
    outcomes: tuple[RuleOutcome, ...],
    worktree: Path,
    changes_diff: bytes,
    rt: Runtime,
    *,
    since_call: int,
    retrievals: tuple[Retrieval, ...] = (),
) -> Found | NotRun:
    """채택한 검사기를 PR이 바꾼 파일에 돌려 바뀐 줄의 위반만 남긴다.

    since_call은 이 검사를 시작하기 전 마지막 호출 기록의 id다. 그 뒤의 기록만 호출 수에 센다.
    retrievals는 검사기 작성이 넣은 과거 기록이다(결과 코멘트에 보인다).
    """
    checks = [_saved_check(o, rt.store) for o in outcomes]
    diff = changes_diff.decode("utf-8", errors="replace")
    try:
        found = detect.detect(
            checks, worktree, added_lines(diff), new_files=new_files(diff), sandbox=rt.sandbox,
            disabled=rt.disabled,
        )  # fmt: skip
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
        note=read.note,
        retrievals=merge_retrievals(retrievals),
        sections=read.sections,
    )


def judge(
    found: Found,
    worktree: Path,
    changes_diff: bytes,
    rt: Runtime,
    *,
    is_stale: Callable[[], bool] = lambda: False,
) -> Found:
    """모델 판단으로 내려온 규칙을 바뀐 hunk에 대해 본다 (4.5). 도구·검사기 다음, 테스트 전이다.

    앞서 모델이 찾은 위반은 버리고 처음부터 다시 판단한다. 저장된 쌍은 호출 없이 같은 답을 쓰므로
    `/fix`가 다시 돌려도 남은 쌍만 묻는다.
    """
    rules_ = [o.rule for o in found.outcomes if isinstance(o, RuleJudged)]
    if not rules_:
        return found
    known = tuple(v for v in found.violations if v.found_by != "model")
    result = model_judge.judge(
        rules_, worktree, changes_diff.decode("utf-8", errors="replace"), store=rt.store,
        budget=rt.budget, model=rt.models.judge, read_code=rt.backend.reading, known=len(known),
        is_stale=is_stale,
    )  # fmt: skip

    def judged(o: RuleOutcome) -> RuleOutcome:
        if not isinstance(o, RuleJudged):
            return o
        rule_id = o.rule.id
        gap, undecided = result.gaps.get(rule_id), result.undecided.get(rule_id)
        return RuleJudged(
            o.rule, o.reason, o.route,
            left=gap.pairs if gap else 0, left_kind=gap.kind if gap else None,
            left_detail=gap.detail if gap else "", pairs=result.pairs.get(rule_id, 0),
            undecided=undecided.pairs if undecided else 0,
            undecided_why=undecided.why if undecided else "",
        )  # fmt: skip

    return replace(
        found, outcomes=tuple(judged(o) for o in found.outcomes),
        violations=known + result.violations, judge=result.usage,
    )  # fmt: skip


def judge_left(found: object) -> bool:
    """호출 상한에 걸려 모델 판단을 다 하지 못한 규칙이 있다. `/fix`가 이어 한다 (4.9 명령)."""
    return isinstance(found, Found) and any(
        isinstance(o, RuleJudged) and o.left_kind == "cap" for o in found.outcomes
    )


def saved_checks(found: Found, store: Store) -> list[RuleCheck]:
    """found의 규칙마다 저장한 검사기. 수정 뒤 재검사(4.8의 5번)가 같은 검사기를 다시 돌린다."""
    return [_saved_check(o, store) for o in found.outcomes]


def _saved_check(outcome: RuleOutcome, store: Store) -> RuleCheck:
    if isinstance(outcome, RuleUnchecked):
        return Unchecked(outcome.rule, outcome.kind, outcome.detail)
    if isinstance(outcome, RuleJudged):  # 도구·검사기로 다시 돌릴 것이 없다 (4.8의 5번)
        return ToJudge(outcome.rule, outcome.route, outcome.reason)
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


def sandbox_for(
    tool_cache: Path, ruff: RuffSetup | None = None, image: str = docker_cli.IMAGE
) -> checkers.FormatsFiles:
    return checkers.Sandbox(tool_cache, ruff, image)


def _first_line(text: str) -> str:
    return text.strip().splitlines()[0] if text.strip() else text
