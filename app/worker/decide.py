"""수정 판단 (docs/DESIGN.md 4.6, 결정 3): 표로 먼저 정하고, 남은 위반만 모델에 묻는다.

모델은 도구 없이 부른다 (`run_structured`, 8.4). 답은 위반마다 `model_cache`에 저장해 같은 위반과
hunk에는 다시 묻지 않는다. 호출은 PR 상한(9.2)에서 센다. 모델 호출이 실패하거나 상한에 걸리면 남은
위반을 `skip`으로 두고 저장하지 않으므로 다음 검사가 다시 묻는다.

이 단계는 정하기만 한다. 문서 규칙 위반의 수정은 아직 잇지 않았다 (13절 2단계의 다음 작업).
"""

import logging
from collections.abc import Sequence
from typing import Final

from app.models.ai_fix import CALL_BUDGET_USD
from app.models.call_budget import Denied, PrScope
from app.models.diff import hunk_around
from app.models.doc_rules import Found
from app.models.fix_decision import (
    ANSWER_SCHEMA,
    HUNK_RADIUS,
    MAX_PER_CALL,
    PROMPT_VERSION,
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
    parse_answer,
    plan,
    reconcile,
)
from app.worker.budget import CallBudget
from app.worker.rules import AskModel
from app.worker.state import Store

log = logging.getLogger(__name__)

CALL_FAILED: Final = "수정 판단을 하지 못함 (모델 호출 실패)"


def decide(
    found: Found,
    tests: SafetyNet,
    changes_diff: bytes,
    *,
    require_covered: bool,
    store: Store,
    budget: CallBudget,
    model: str,
    ask: AskModel,
    per_call: int = MAX_PER_CALL,
) -> Decided:
    """found의 위반마다 수정 판단 하나. model은 `model.decide`다. per_call개씩 묶어 묻는다."""
    rules = {o.rule.id: o.rule for o in found.outcomes}
    planned = plan(
        found.violations, {rid: r.fix for rid, r in rules.items()}, tests,
        require_covered=require_covered,
    )  # fmt: skip
    decided = {d.violation_id: d for d in planned.decided}
    diff = changes_diff.decode("utf-8", errors="replace")
    questions = [
        Question(v, rules[v.rule_id], hunk_around(diff, v.file, v.line, HUNK_RADIUS))
        for v in planned.for_model
    ]
    keys = {q.violation.id: decision_key(q, model) for q in questions}
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
    calls = 0
    for start in range(0, len(to_ask), per_call):
        chunk = to_ask[start : start + per_call]
        answers, called = _ask(chunk, keys, store=store, budget=budget, model=model, ask=ask)
        calls += called
        decided |= {d.violation_id: d for d in answers}
    return Decided(tuple(decided[v.id] for v in found.violations), calls=calls, cached=cached)


def _ask(
    chunk: Sequence[Question],
    keys: dict[str, str],
    *,
    store: Store,
    budget: CallBudget,
    model: str,
    ask: AskModel,
) -> tuple[tuple[FixDecision, ...], int]:
    """위반 묶음 하나를 묻는다. (판단들, 상한에서 센 호출 수)."""
    asked = [q.violation for q in chunk]
    call = budget.begin(PrScope(), stage="decide", tier="light", model=model)
    if isinstance(call, Denied):
        log.info("수정 판단을 하지 않음: %s", call.reason)
        return not_decided(asked, "call_limit", f"수정 판단을 하지 못함 ({call.reason})"), 0
    answer = ask(
        build_prompt(chunk),
        system_prompt=SYSTEM_PROMPT,
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
    if failure is not None:
        log.warning("수정 판단 실패: %s", failure)
        return not_decided(asked, "undecided", CALL_FAILED), 1
    decisions = reconcile(asked, parsed)
    for d in decisions:
        store.save_result(
            keys[d.violation_id], stage="decide", model=answer.model_id or model,
            prompt_version=PROMPT_VERSION, result_json=decision_to_json(d),
        )  # fmt: skip
    return decisions, 1
