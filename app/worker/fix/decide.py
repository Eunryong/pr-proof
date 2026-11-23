"""수정 판단 (docs/DESIGN.md 4.6, 결정 3): 표로 먼저 정하고, 남은 위반만 모델에 묻는다.

모델은 도구 없이 부른다 (`ModelBackend.ask`, 8.4). 답은 위반마다 `model_cache`에 저장해 같은 위반과
hunk에는 다시 묻지 않는다. 호출은 PR 상한(9.2)에서 센다. 모델 호출이 실패하거나 상한에 걸리면 남은
위반을 `skip`으로 두고 저장하지 않으므로 다음 검사가 다시 묻는다.

이 단계는 정하기만 한다. `fix`·`model`로 정한 위반은 app/worker/fix/model_fix.py가 고친다.
"""

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Final

from app.worker.core.diff_pure import hunk_around
from app.worker.core.violation_pure import Violation
from app.worker.doc_rules_pure import Found, outcomes_by_rule
from app.worker.fix.fix_decision_pure import (
    ANSWER_SCHEMA,
    HUNK_RADIUS,
    MAX_PER_CALL,
    PROMPT_VERSION,
    RECORDS_NOTE,
    SYSTEM_PROMPT,
    Decided,
    FixDecision,
    Question,
    SafetyNet,
    build_prompt,
    decision_from_json,
    decision_key,
    decision_to_json,
    not_decided,
    open_targets,
    parse_answer,
    plan,
    reconcile,
    with_records,
)
from app.worker.model.model_backend import AskModel
from app.worker.store.budget import CallBudget
from app.worker.store.call_budget_pure import CALL_BUDGET_USD, Denied, PrScope
from app.worker.store.memory import FindsRecords
from app.worker.store.rag_pure import (
    EMPTY,
    Retrieval,
    Selection,
    merge_retrievals,
    query_text,
    render_records,
    retrieval,
)
from app.worker.store.state import Store

log = logging.getLogger(__name__)

CALL_FAILED: Final = "수정 판단을 하지 못함 (모델 호출 실패)"


def decide(
    found: Found,
    tests: SafetyNet,
    changes_diff: bytes,
    *,
    store: Store,
    budget: CallBudget,
    model: str,
    ask: AskModel,
    per_call: int = MAX_PER_CALL,
    retriever: FindsRecords | None = None,
    context_of: Callable[[Violation], str] = lambda v: "",
) -> Decided:
    """found의 위반마다 수정 판단 하나. model은 `model.decide`다. per_call개씩 묶어 묻는다.

    retriever가 있으면(검색을 켬) 모델에 넘기는 위반마다 같은 저장소의 채택·거절·실패 기록을 찾아
    프롬프트에 참고 자료로 넣는다 (6.2). 한 호출의 기록은 `rag.max_records`건까지이므로, 고른 기록이
    같은 위반끼리 묶어 묻는다. context_of(위반)는 위반 줄과 앞뒤 5줄이다(질의 글, 6.6의 4번).
    """
    rules = {rule_id: o.rule for rule_id, o in outcomes_by_rule(found).items()}
    planned = plan(found.violations, {rid: r.fix for rid, r in rules.items()}, tests)
    decided = {d.violation_id: d for d in planned.decided}
    budget = budget.with_targets(open_targets(planned.decided) + len(planned.for_model))
    diff = changes_diff.decode("utf-8", errors="replace")
    questions = [
        Question(v, rules[v.rule_id], hunk_around(diff, v.file, v.line, HUNK_RADIUS))
        for v in planned.for_model
    ]
    chosen = {q.violation.id: _records_for(q, retriever, context_of) for q in questions}
    keys = {q.violation.id: decision_key(q, model, chosen[q.violation.id].ids) for q in questions}
    to_ask: list[Question] = []
    for q in questions:
        entry = store.cached_result(keys[q.violation.id])
        if entry is None:
            to_ask.append(q)
        else:
            decided[q.violation.id] = decision_from_json(q.violation.id, entry.result_json)
    cached = len(questions) - len(to_ask)
    if cached:
        budget.record_hit(PrScope(), stage="decide", tier="light", model=model)
    calls = input_tokens = output_tokens = 0
    groups: dict[tuple[int, ...], list[Question]] = {}
    for q in to_ask:  # 같은 기록을 넣는 위반끼리 (기록이 없는 위반은 한 묶음)
        groups.setdefault(chosen[q.violation.id].ids, []).append(q)
    for group in groups.values():
        records = render_records(chosen[group[0].violation.id].chosen)
        for start in range(0, len(group), per_call):
            chunk = group[start : start + per_call]
            asked = _ask(chunk, keys, store=store, budget=budget, model=model, ask=ask,
                         records=records)  # fmt: skip
            calls += asked.calls
            input_tokens += asked.input_tokens
            output_tokens += asked.output_tokens
            decided |= {d.violation_id: d for d in asked.decisions}
    for q in questions:  # 저장된 답에도 참고한 기록을 붙인다 (키에 그 기록이 들어 있다)
        sel = chosen[q.violation.id]
        decided[q.violation.id] = with_records(decided[q.violation.id], sel.ids, _rejected(sel))
    return Decided(
        tuple(decided[v.id] for v in found.violations), calls=calls, cached=cached,
        input_tokens=input_tokens, output_tokens=output_tokens,
        retrieval=_retrieval(chosen.values(), retriever),
    )  # fmt: skip


def _records_for(
    q: Question, retriever: FindsRecords | None, context_of: Callable[[Violation], str]
) -> Selection:
    if retriever is None:
        return EMPTY
    return retriever.find("decide", [query_text(q.rule.text, context_of(q.violation))])


def _rejected(selection: Selection) -> frozenset[int]:
    return frozenset(c.record.id or 0 for c in selection.chosen if c.record.kind == "rejected")


def _retrieval(selections: Iterable[Selection], retriever: FindsRecords | None) -> Retrieval | None:
    if retriever is None:
        return None
    merged = merge_retrievals([retrieval(s, "decide") for s in selections])
    found = merged[0] if merged else Retrieval("decide")
    return replace(found, error=retriever.error)


@dataclass(frozen=True)
class _Asked:
    decisions: tuple[FixDecision, ...]
    calls: int  # 상한에서 센 호출 수
    input_tokens: int = 0
    output_tokens: int = 0


def _ask(
    chunk: Sequence[Question],
    keys: dict[str, str],
    *,
    store: Store,
    budget: CallBudget,
    model: str,
    ask: AskModel,
    records: str = "",
) -> _Asked:
    """위반 묶음 하나를 묻는다. records는 이 묶음에 넣는 과거 기록 블록이다(없으면 "")."""
    asked = [q.violation for q in chunk]
    call = budget.begin(PrScope(), stage="decide", tier="light", model=model)
    if isinstance(call, Denied):
        log.info("수정 판단을 하지 않음: %s", call.reason)
        return _Asked(not_decided(asked, "call_limit", f"수정 판단을 하지 못함 ({call.reason})"), 0)
    answer = ask(
        build_prompt(chunk, records),
        system_prompt=SYSTEM_PROMPT + (RECORDS_NOTE if records else ""),
        schema=ANSWER_SCHEMA,
        model=model,
        max_budget_usd=CALL_BUDGET_USD["light"],
    )
    failure = None if answer.ok else f"{answer.error}: {answer.detail}"
    parsed: tuple[FixDecision, ...] = ()
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
    tokens = {"input_tokens": answer.input_tokens or 0, "output_tokens": answer.output_tokens or 0}
    if failure is not None:
        log.warning("수정 판단 실패: %s", failure)
        return _Asked(not_decided(asked, "undecided", CALL_FAILED), 1, **tokens)
    decisions = reconcile(asked, parsed)
    for d in decisions:
        store.save_result(
            keys[d.violation_id], stage="decide", model=answer.model_id or model,
            prompt_version=PROMPT_VERSION, result_json=decision_to_json(d),
        )  # fmt: skip
    return _Asked(decisions, 1, **tokens)
