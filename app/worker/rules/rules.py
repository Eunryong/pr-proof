"""규칙 읽기 (docs/DESIGN.md 4.3): base의 규칙 문서를 모델로 읽어 원문을 확인한 규칙 묶음을 만든다.

문서는 PR head가 아니라 base 커밋의 blob에서 읽는다. 결과는 `rulesets`에 캐시 키별로 저장해
같은 문서에는 모델을 다시 부르지 않는다. 호출은 규칙 묶음 상한(9.2)에서 센다. 모델 호출이
실패하거나 상한에 걸리면 저장하지 않으므로 다음 검사가 다시 시도한다.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.worker.ai.model_backend import AskModel
from app.worker.config.repo_config_pure import RulesSettings
from app.worker.core.diff_pure import added_lines
from app.worker.core.rule_pure import Rule, RuleSet, UnreadDoc
from app.worker.git import repo
from app.worker.git.git_pure import REGULAR_FILE_MODES, TreeEntry
from app.worker.rule_reading_pure import (
    ANSWER_SCHEMA,
    MAX_DOCS_BYTES,
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
from app.worker.rules import doc_sections_pure
from app.worker.store.budget import CallBudget
from app.worker.store.call_budget_pure import CALL_BUDGET_USD, Denied, RulesetScope
from app.worker.store.state import Store

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RulesFound:
    key: str
    ruleset: RuleSet  # 저장한 묶음 그대로 (`rules.disabled` 적용 전)
    rules: tuple[Rule, ...]  # 적용할 규칙 (`rules.disabled`를 뺀 것). 다 껐으면 비어 있다
    cached: bool
    sections: tuple[str, ...] = ()  # 큰 문서에서 읽은 절 (6.3). 문서를 전부 읽었으면 비어 있다


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


class FindsSections(Protocol):
    """큰 규칙 문서의 조각을 찾는 검색 (6.3). 검색을 켠 저장소만 있다
    (app/worker/store/memory.Retriever)."""

    def find_sections(
        self, chunks: Sequence[tuple[str, str]], query: str
    ) -> list[tuple[str, float]]: ...


def saved_rules(
    bare: Path, base_sha: str, settings: RulesSettings, *, model: str, store: Store
) -> RulesResult | None:
    """worktree 없이 base의 규칙 문서 목록으로 저장된 규칙 묶음을 찾는다 (4.11). 저장된 것이 없으면
    None. 모델을 부르지 않고 문서도 읽지 않는다. 키는 `load_rules`와 같다(문서의 blob 해시와 모델).
    """
    selection = select_documents(settings.docs, repo.bare_entries(bare, base_sha, settings.docs))
    if not selection.listed:
        return NoRuleDocuments(unread=())
    key = ruleset_key(selection.listed, model)
    saved = store.saved_ruleset(key)
    if saved is None:
        return None
    return _result(key, RuleSet.from_json(saved), settings.disabled, cached=True)


def load_rules(
    worktree: Path,
    base_sha: str,
    settings: RulesSettings,
    *,
    model: str,
    store: Store,
    budget: CallBudget,
    ask: AskModel,
    sections: FindsSections | None = None,
    changes_diff: bytes = b"",
) -> RulesResult:
    """base_sha의 규칙 문서를 읽어 규칙 묶음을 돌려준다. model은 `model.rules`다.

    문서 합계가 400KB를 넘고 sections(검색)가 있으면 그 PR과 관련된 절만 읽는다 (6.3,
    `_load_sections`). changes_diff는 PR의 diff이고 관련 절을 찾는 질의가 된다. 검색이 없으면
    400KB를 넘는 문서는 읽지 않고 이유를 남긴다.
    """
    entries = repo.list_base_entries(worktree, base_sha, settings.docs)
    selection = select_documents(settings.docs, entries)
    if not selection.listed:
        return NoRuleDocuments(unread=())
    key = ruleset_key(selection.listed, model)
    scope = RulesetScope(key)
    if sections is not None and _too_big(selection.listed):
        large = _load_sections(
            worktree, selection, key, settings, model=model, store=store, budget=budget,
            ask=ask, sections=sections, changes_diff=changes_diff,
        )  # fmt: skip
        if large is not None:
            return large

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


def _too_big(listed: Sequence[TreeEntry]) -> bool:
    return sum(e.size or 0 for e in listed if e.mode in REGULAR_FILE_MODES) > MAX_DOCS_BYTES


def _load_sections(
    worktree: Path,
    selection: DocSelection,
    key: str,
    settings: RulesSettings,
    *,
    model: str,
    store: Store,
    budget: CallBudget,
    ask: AskModel,
    sections: FindsSections,
    changes_diff: bytes,
) -> RulesResult | None:
    """큰 규칙 문서 (6.3): 제목 단위 조각을 임베딩하고(바뀐 조각만), 늘 읽는 절·적용 범위로 고른 절·
    검색이 찾은 절을 400KB 안에서 고른다. 읽어 둔 조각의 규칙은 조각 해시로 저장해 다시 읽지 않고,
    처음 보는 조각만 모은 발췌를 모델이 읽는다. 검색을 하지 못하면 None(400KB까지만 읽는
    방식으로)."""
    wide = select_documents(settings.docs, {e.path: e for e in selection.listed},
                            max_bytes=doc_sections_pure.MAX_LARGE_DOCS_BYTES)  # fmt: skip
    documents, unread = _read(worktree, wide)
    all_chunks = [c for d in documents for c in doc_sections_pure.chunks(d)]
    diff = changes_diff.decode("utf-8", errors="replace")
    changed = sorted(added_lines(diff))
    added = "\n".join(line[1:] for line in diff.splitlines()
                      if line.startswith("+") and not line.startswith("+++"))  # fmt: skip
    try:
        hits = sections.find_sections(
            [(c.id, c.embed_text) for c in all_chunks], doc_sections_pure.query_text(changed, added)
        )
    except Exception as e:  # noqa: BLE001 (임베딩·Chroma의 실패는 400KB까지 읽는 방식으로 넘긴다)
        log.warning("큰 규칙 문서의 절을 찾지 못함: %s", e)
        return None
    saved: dict[str, RuleSet] = {}
    for c in all_chunks:
        text = store.saved_ruleset(doc_sections_pure.section_key(c, model))
        if text is not None:
            saved[c.id] = RuleSet.from_json(text)
    picked = doc_sections_pure.pick(
        all_chunks, scoped=doc_sections_pure.scoped_to(saved, changed), hits=hits,
    )  # fmt: skip
    todo = [c for c in picked if c.id not in saved]
    scope = RulesetScope(key)
    dropped: tuple = ()
    answered_by = model
    if todo:
        call = budget.begin(scope, stage="rules", tier="light", model=model)
        if isinstance(call, Denied):
            log.info("규칙 읽기를 하지 않음: %s", call.reason)
            return CapReached(key, call)
        paths = {c.path for c in todo}
        excerpts = tuple(doc_sections_pure.excerpt(d, todo) for d in documents if d.path in paths)
        answer = ask(build_prompt(excerpts), system_prompt=SYSTEM_PROMPT, schema=ANSWER_SCHEMA,
                     model=model, max_budget_usd=CALL_BUDGET_USD["light"])  # fmt: skip
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
        answered_by = answer.model_id or model
        rules, dropped = verify_rules(parsed, excerpts)
        by_chunk: dict[str, list[Rule]] = {c.id: [] for c in todo}
        for rule in rules:
            home = doc_sections_pure.chunk_of(rule, todo)
            if home is not None:
                by_chunk[home.id].append(rule)
        for c in todo:
            part = RuleSet(rules=tuple(by_chunk[c.id]), sources=((c.path, c.blob_sha),),
                           pipeline_version=READER_VERSION, prompt_version=PROMPT_VERSION,
                           model=answered_by)  # fmt: skip
            store.save_ruleset(doc_sections_pure.section_key(c, model), part.to_json())
            saved[c.id] = part
    else:
        budget.record_hit(scope, stage="rules", tier="light", model=model)
    merged: dict[str, Rule] = {}
    for c in picked:
        for rule in saved[c.id].rules:
            merged.setdefault(rule.id, rule)
    ruleset = RuleSet(
        rules=tuple(merged.values()), sources=tuple((d.path, d.blob_sha) for d in documents),
        pipeline_version=READER_VERSION, prompt_version=PROMPT_VERSION, model=answered_by,
        unread=unread, dropped=dropped,
    )  # fmt: skip
    if not ruleset.rules:
        return NoRules(key, ruleset, not todo)
    return RulesFound(key, ruleset, apply_disabled(ruleset.rules, settings.disabled), not todo,
                      sections=tuple(c.label for c in picked))  # fmt: skip


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
