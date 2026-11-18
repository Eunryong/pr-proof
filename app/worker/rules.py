"""규칙 읽기 (docs/DESIGN.md 4.3): base의 규칙 문서를 모델로 읽어 원문을 확인한 규칙 묶음을 만든다.

문서는 PR head가 아니라 base 커밋의 blob에서 읽는다. 결과는 `rulesets`에 캐시 키별로 저장해
같은 문서에는 모델을 다시 부르지 않는다. 호출은 규칙 묶음 상한(9.2)에서 센다. 모델 호출이
실패하거나 상한에 걸리면 저장하지 않으므로 다음 검사가 다시 시도한다.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.models.ai_fix import CALL_BUDGET_USD
from app.models.call_budget import Denied, RulesetScope
from app.models.repo_config import RulesSettings
from app.models.rule import Rule, RuleSet, UnreadDoc
from app.models.rule_reading import (
    ANSWER_SCHEMA,
    PROMPT_VERSION,
    READER_VERSION,
    SYSTEM_PROMPT,
    DocSelection,
    Document,
    apply_disabled,
    build_prompt,
    parse_answer,
    ruleset_key,
    select_documents,
    verify_rules,
)
from app.worker import repo
from app.worker.ai import claude_cli
from app.worker.ai.claude_cli import StructuredResult
from app.worker.budget import CallBudget
from app.worker.state import Store

log = logging.getLogger(__name__)

# (사용자 메시지, *, system_prompt, schema, model, max_budget_usd) → 결과. 테스트는 가짜를 넘긴다
AskModel = Callable[..., StructuredResult]


@dataclass(frozen=True)
class RulesFound:
    key: str
    ruleset: RuleSet  # 저장한 묶음 그대로 (`rules.disabled` 적용 전)
    rules: tuple[Rule, ...]  # 적용할 규칙 (`rules.disabled`를 뺀 것). 다 껐으면 비어 있다
    cached: bool


@dataclass(frozen=True)
class NoRuleDocuments:
    """base에 읽을 규칙 문서가 없다. 있었지만 읽지 못한 문서는 unread에 이유와 함께 있다."""

    unread: tuple[UnreadDoc, ...]


@dataclass(frozen=True)
class NoRules:
    """문서는 읽었지만 원문을 확인한 규칙이 없다. 모델이 답한 규칙을 버렸으면 ruleset.dropped에."""

    key: str
    ruleset: RuleSet
    cached: bool


@dataclass(frozen=True)
class ModelFailed:
    key: str
    reason: str


@dataclass(frozen=True)
class CapReached:
    key: str
    denied: Denied


RulesResult = RulesFound | NoRuleDocuments | NoRules | ModelFailed | CapReached


def load_rules(
    worktree: Path,
    base_sha: str,
    settings: RulesSettings,
    *,
    model: str,
    store: Store,
    budget: CallBudget,
    ask: AskModel = claude_cli.run_structured,
) -> RulesResult:
    """base_sha의 규칙 문서를 읽어 규칙 묶음을 돌려준다. model은 가벼운 모델(`model.light`)이다."""
    entries = repo.list_base_entries(worktree, base_sha, settings.docs)
    selection = select_documents(settings.docs, entries)
    if not selection.listed:
        return NoRuleDocuments(unread=())
    key = ruleset_key(selection.listed, model)
    scope = RulesetScope(key)

    saved = store.saved_ruleset(key)
    if saved is not None:
        budget.record_hit(scope, stage="rules", tier="light", model=model)
        return _result(key, RuleSet.from_json(saved), settings.disabled, cached=True)

    documents, unread = _read(worktree, selection)
    if not documents:
        return NoRuleDocuments(unread=unread)

    call = budget.begin(scope, stage="rules", tier="light", model=model)
    if isinstance(call, Denied):
        log.info("규칙 읽기를 하지 않음: %s", call.reason)
        return CapReached(key, call)
    answer = ask(
        build_prompt(documents),
        system_prompt=SYSTEM_PROMPT,
        schema=ANSWER_SCHEMA,
        model=model,
        max_budget_usd=CALL_BUDGET_USD["light"],
    )
    failure = None if answer.ok else f"{answer.error}: {answer.detail}"
    if failure is None:
        try:
            parsed = parse_answer(answer.data or {})
        except ValueError as e:
            failure = f"답의 형식이 틀림: {str(e).splitlines()[0]}"
    budget.finish(
        call, ok=failure is None, error=failure, model_id=answer.model_id,
        input_tokens=answer.input_tokens, output_tokens=answer.output_tokens,
        permission_denials=answer.permission_denials,
    )  # fmt: skip
    if failure is not None:
        log.warning("규칙 읽기 실패: %s", failure)
        return ModelFailed(key, failure)

    rules, dropped = verify_rules(parsed, documents)
    for d in dropped:
        log.info("규칙을 버림 (%s): %s > %s: %s", d.reason, d.file, d.section, d.quote)
    ruleset = RuleSet(
        rules=rules,
        sources=tuple((d.path, d.blob_sha) for d in documents),
        pipeline_version=READER_VERSION,
        prompt_version=PROMPT_VERSION,
        model=answer.model_id or model,
        unread=unread,
        dropped=dropped,
    )
    store.save_ruleset(key, ruleset.to_json())
    return _result(key, ruleset, settings.disabled, cached=False)


def _read(
    worktree: Path, selection: DocSelection
) -> tuple[tuple[Document, ...], tuple[UnreadDoc, ...]]:
    documents, unread = [], list(selection.unread)
    for entry in selection.to_read:
        try:
            text = repo.read_blob(worktree, entry.sha)
        except UnicodeDecodeError:
            unread.append(UnreadDoc(entry.path, "UTF-8 글이 아님"))
            continue
        documents.append(Document(entry.path, entry.sha, text))
    return tuple(documents), tuple(unread)


def _result(key: str, ruleset: RuleSet, disabled: list[str], *, cached: bool) -> RulesResult:
    if not ruleset.rules:
        return NoRules(key, ruleset, cached)
    return RulesFound(key, ruleset, apply_disabled(ruleset.rules, disabled), cached)
