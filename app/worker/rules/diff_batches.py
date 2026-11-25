"""큰 변경의 묶음마다 모델을 같이 부른다 (docs/DESIGN.md 4.5 끝 "큰 변경"). 나누는 것과 프롬프트는
app/worker/rules/diff_batches_pure.py가 한다.

호출은 `PARALLEL`개까지 같이 돈다. 같이 도는 줄마다 읽기 폴더를 따로 연다: 폴더 하나의 경로 감시
훅 기록은 호출 하나씩 세므로 두 호출이 같은 폴더를 같이 쓰면 섞인다. 호출 상한의 기록과 저장은
부르는 쪽 스레드에서만 한다.
"""

import logging
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Final

from app.worker.model.model_backend import ReadCode, StructuredResult
from app.worker.rules.diff_batches_pure import (
    DIGEST_SCHEMA,
    DIGEST_SYSTEM,
    DIGEST_VERSION,
    Batch,
    Split,
    changes_from_json,
    changes_to_json,
    digest_key,
    digest_prompt,
    parse_changes,
)
from app.worker.rules.pr_description_pure import CALL_BUDGET_USD, MAX_TOOL_USES
from app.worker.store.budget import CallBudget
from app.worker.store.call_budget_pure import Denied, PrScope, Stage
from app.worker.store.state import Store

log = logging.getLogger(__name__)

PARALLEL: Final = 4  # 같이 도는 호출 수. 그만큼 읽기 폴더(worktree의 사본)가 열린다
DIGEST_STAGE: Final = "diff_digest"  # 저장한 요약의 단계 이름 (`model_cache`)
UNUSED: Final = "호출 상한으로 쓰지 않음"


def ask_all(
    read_code: ReadCode, worktree: Path, prompts: Sequence[str], **kwargs: object
) -> list[StructuredResult]:
    """prompts를 차례대로 물은 답. 줄 `PARALLEL`개로 나눠 같이 돌리고 줄마다 폴더 하나를 쓴다."""
    lanes = min(PARALLEL, len(prompts))
    if lanes == 0:
        return []

    def run(lane: int) -> list[tuple[int, StructuredResult]]:
        with read_code(worktree) as ask:
            return [(i, ask(prompts[i], **kwargs)) for i in range(lane, len(prompts), lanes)]

    with ThreadPoolExecutor(max_workers=lanes) as pool:
        answered = dict(pair for pairs in pool.map(run, range(lanes)) for pair in pairs)
    return [answered[i] for i in range(len(prompts))]


def begin_calls(
    budget: CallBudget, count: int, *, stage: Stage, model: str
) -> tuple[list[int], Denied | None]:
    """호출 count개를 상한에 적는다. 상한에 걸리면 거기까지 적은 것과 걸린 까닭을 준다."""
    ids: list[int] = []
    for _ in range(count):
        call = budget.begin(PrScope(), stage=stage, tier="light", model=model)
        if isinstance(call, Denied):
            return ids, call
        ids.append(call)
    return ids, None


def finish_call(
    budget: CallBudget, call: int, answer: StructuredResult, failure: str | None
) -> None:
    budget.finish(
        call, ok=failure is None, error=failure, model_id=answer.model_id,
        input_tokens=answer.input_tokens, output_tokens=answer.output_tokens,
        permission_denials=answer.permission_denials,
    )  # fmt: skip


def saved_digests(store: Store, parts: Split, model: str) -> list[tuple[str, ...] | None]:
    """묶음마다 저장해 둔 요약. 없거나 읽지 못한 묶음은 None이다."""
    out: list[tuple[str, ...] | None] = []
    for batch in parts.batches:
        entry = store.cached_result(digest_key(batch, model))
        try:
            out.append(changes_from_json(entry.result_json) if entry is not None else None)
        except ValueError as e:
            log.warning("저장한 묶음 요약을 읽지 못해 다시 물음: %s", e)
            out.append(None)
    return out


def save_digest(
    store: Store, batch: Batch, changes: Sequence[str], model: str, model_id: str = ""
) -> None:
    """model은 키에 넣는 설정의 모델 이름, model_id는 답한 모델의 전체 id다."""
    store.save_result(
        digest_key(batch, model), stage=DIGEST_STAGE, model=model_id or model,
        prompt_version=DIGEST_VERSION, result_json=changes_to_json(changes),
    )  # fmt: skip


def ask_digests(
    parts: Split, indices: Sequence[int], calls: Sequence[int], worktree: Path, *, store: Store,
    budget: CallBudget, read_code: ReadCode, model: str,
) -> tuple[dict[int, tuple[str, ...]], list[StructuredResult]]:  # fmt: skip
    """indices의 묶음마다 요약만 받는 호출을 같이 돌린다. calls는 묶음마다 상한에 적어 둔 호출이다.
    받은 요약(묶음 번호 → 줄들)은 저장하고, 실패한 묶음은 빼고 로그에 남긴다. 답도 같이 준다(쓴
    양을 세는 데 쓴다)."""
    answers = ask_all(
        read_code, worktree, [digest_prompt(parts, i) for i in indices],
        system_prompt=DIGEST_SYSTEM, schema=DIGEST_SCHEMA, model=model,
        max_budget_usd=CALL_BUDGET_USD, max_tool_uses=MAX_TOOL_USES,
    )  # fmt: skip
    got: dict[int, tuple[str, ...]] = {}
    for index, call, answer in zip(indices, calls, answers, strict=True):
        failure = None if answer.ok else f"{answer.error}: {answer.detail}"
        if failure is None:
            try:
                got[index] = parse_changes(answer.data or {})
            except ValueError as e:
                failure = f"답의 형식이 틀림: {str(e).splitlines()[0]}"
        finish_call(budget, call, answer, failure)
        if failure is not None:
            log.warning("묶음 %d의 요약을 받지 못함: %s", index + 1, failure)
        else:
            save_digest(store, parts.batches[index], got[index], model, answer.model_id)
    return got, answers
