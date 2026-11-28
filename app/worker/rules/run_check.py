"""실행 결과의 규칙 (docs/DESIGN.md 4.5 "실행 결과의 규칙"): 테스트 뒤에 run 규칙마다 모델이 정한다.

그래프의 `test` node가 테스트를 돌린 뒤 부른다. 규칙마다 호출 하나이고 도구 없이
부른다(`ModelBackend.ask`). 호출은 PR 상한(9.2)에서 세고 답은 (규칙, 실행 결과의 해시)로 저장한다.
같은 실행 결과에는 다시 묻지 않는다. 상한에 걸리거나 호출이 실패하면 그 규칙은 "묻지 못함"이고
저장하지 않는다. 어겼다는 답은 알리기만 한다: 줄이 아니라 PR에 걸려 수정 대상이 되지 않는다(12.2).
"""

import logging
from collections.abc import Callable

from app.worker.rules.checkers import STALE
from app.worker.rules.doc_rules import Runtime
from app.worker.rules.doc_rules_pure import Found, RunJudged
from app.worker.rules.run_check_pure import (
    ANSWER_SCHEMA,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    answer_from_json,
    answer_to_json,
    build_prompt,
    facts_for,
    read_answer,
    run_key,
    run_rules,
    without_env,
)
from app.worker.store.call_budget_pure import CALL_BUDGET_USD, Denied, PrScope
from app.worker.testing.test_run_pure import TestRun

log = logging.getLogger(__name__)


def judge_runs(
    found: Found,
    run: TestRun,
    rt: Runtime,
    *,
    is_stale: Callable[[], bool] = lambda: False,
) -> tuple[RunJudged, ...]:
    """run 규칙마다 실행 결과를 모델에게 보여 지켰는지 정한다. run 규칙이 없으면 빈 튜플."""
    rules = run_rules(found)
    if not rules:
        return ()
    unasked = without_env(found, run)
    if unasked is not None:
        return unasked
    facts = facts_for(found, run)
    model = rt.models.judge
    out = []
    for rule in rules:
        key = run_key(rule, facts, model)
        entry = rt.store.cached_result(key)
        if entry is not None:
            try:
                answer = answer_from_json(entry.result_json)
            except ValueError:
                entry = None
            else:
                rt.budget.record_hit(PrScope(), stage="run_judge", tier="light", model=model)
        if entry is None:
            if is_stale():
                out.append(RunJudged(rule.id, "left", STALE, facts.summary()))
                continue
            call = rt.budget.begin(PrScope(), stage="run_judge", tier="light", model=model)
            if isinstance(call, Denied):
                out.append(RunJudged(rule.id, "left", call.reason, facts.summary()))
                continue
            result = rt.backend.ask(build_prompt(rule, facts), system_prompt=SYSTEM_PROMPT,
                                    schema=ANSWER_SCHEMA, model=model,
                                    max_budget_usd=CALL_BUDGET_USD["light"])  # fmt: skip
            failure = None if result.ok else f"{result.error}: {result.detail}"
            if failure is None:
                try:
                    answer = read_answer(result.data or {})
                except ValueError as e:
                    failure = f"답의 형식이 틀림: {str(e).splitlines()[0]}"
            rt.budget.finish(
                call, ok=failure is None, error=failure, model_id=result.model_id,
                input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                permission_denials=result.permission_denials,
            )  # fmt: skip
            if failure is not None:
                log.warning("실행 결과의 규칙 판단 실패 (%s): %s", rule.id, failure)
                out.append(RunJudged(rule.id, "left", failure, facts.summary()))
                continue
            rt.store.save_result(key, stage="run_judge", model=result.model_id or model,
                                 prompt_version=PROMPT_VERSION,
                                 result_json=answer_to_json(answer))  # fmt: skip
        out.append(RunJudged(rule.id, answer.verdict, answer.why, facts.summary()))
    return tuple(out)
