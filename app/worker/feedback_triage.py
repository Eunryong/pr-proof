"""PR 코멘트의 판단 호출 (docs/DESIGN.md 4.11). 수정 판단(fix/decide.py)과 같은 방식이다.

모델은 도구 없이 부른다(`ModelBackend.ask`). 답은 사용자 메시지 전체의 해시로 `model_cache`에 저장해
같은 입력이면 다시 묻지 않는다. 호출은 PR 상한(9.2)에서 단계 `feedback`으로 센다. 호출이 실패하거나
상한에 걸리면 넘긴 코멘트 모두 error이고 저장하지 않는다(새 커밋에서 다시 묻는다).
"""

import json
import logging
from collections.abc import Mapping, Sequence

from app.worker.core.rule_pure import Rule
from app.worker.feedback_pure import Item
from app.worker.feedback_triage_pure import (
    ANSWER_SCHEMA,
    CALL_FAILED,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    Facts,
    KnownFix,
    Triage,
    build_prompt,
    failed,
    parse_answer,
    triage_key,
)
from app.worker.model.model_backend import AskModel
from app.worker.store.budget import CallBudget
from app.worker.store.call_budget_pure import CALL_BUDGET_USD, Denied, PrScope
from app.worker.store.state import Store

log = logging.getLogger(__name__)


def triage(
    items: Sequence[Item],
    *,
    rules: Sequence[Rule],
    rules_note: str,
    facts: Facts,
    code: Mapping[int, str],
    fixes: Sequence[KnownFix],
    store: Store,
    budget: CallBudget,
    model: str,
    ask: AskModel,
) -> dict[int, Triage]:
    """코멘트마다 판단 하나 (item_id → 판단). 넘긴 코멘트 모두 답이 있다. model은
    `model.decide`다."""
    if not items:
        return {}
    prompt = build_prompt(rules, rules_note, facts, items, code, fixes)
    key = triage_key(prompt, model)
    entry = store.cached_result(key)
    if entry is not None:
        budget.record_hit(PrScope(), stage="feedback", tier="light", model=model)
        return parse_answer(json.loads(entry.result_json), items, fixes)
    call = budget.begin(PrScope(), stage="feedback", tier="light", model=model)
    if isinstance(call, Denied):
        log.info("코멘트 판단을 하지 않음: %s", call.reason)
        return failed(items, f"코멘트 판단을 하지 않음 ({call.reason})")
    answer = ask(
        prompt, system_prompt=SYSTEM_PROMPT, schema=ANSWER_SCHEMA, model=model,
        max_budget_usd=CALL_BUDGET_USD["light"],
    )  # fmt: skip
    failure = None if answer.ok else f"{answer.error}: {answer.detail}"
    decided: dict[int, Triage] = {}
    if failure is None:
        try:
            decided = parse_answer(answer.data or {}, items, fixes)
        except ValueError as e:
            failure = f"답의 형식이 틀림: {str(e).splitlines()[0]}"
    budget.finish(
        call, ok=failure is None, error=failure, model_id=answer.model_id,
        input_tokens=answer.input_tokens, output_tokens=answer.output_tokens,
        permission_denials=answer.permission_denials,
    )  # fmt: skip
    if failure is not None:
        log.warning("코멘트 판단 실패: %s", failure)
        return failed(items, CALL_FAILED)
    store.save_result(
        key, stage="feedback", model=answer.model_id or model, prompt_version=PROMPT_VERSION,
        result_json=json.dumps(answer.data, ensure_ascii=False),
    )  # fmt: skip
    return decided
