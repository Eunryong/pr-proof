"""PR 설명 확인 (docs/DESIGN.md 4.5 끝 "PR 설명과 diff 견주기"): PR이 글로 말한 것과 실제로 바뀐
것이 맞는지 모델이 견주고, 어긋난 곳을 결과 코멘트에 알린다. 고치지 않는다.

그래프의 `pr_description` node가 PR 단위 확인 다음, 테스트 전에 부른다(규칙이 없어도 돈다).
가드레일(8.6)이 들인 작업에서만 이 node까지 오고, 그때만 PR 글을 읽는다.

모델은 위반 판단과 같은 장치로 부른다: worktree의 일반 파일을 복사한 모델 폴더를 cwd로 읽기
도구(Read, Grep, Glob)만 주고(`workdir.reading_folder`, `read_tools` 모양, 8.4), 폴더는 물을 때만
연다. 호출은 PR 한 번에 하나이고 PR 상한(9.2)에서 센다. 답은 (보인 PR 글, diff, 파이프라인 수정안의
변경, 모델, 프롬프트 판)의 키로 모델이 읽은 경로의 해시와 함께 저장하고, 그 경로가 모두 그대로일
때만 다시 쓴다. 호출 상한, 실패는 저장하지 않는다.
"""

import logging
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from app.worker import repo
from app.worker.budget import CallBudget
from app.worker.call_budget_pure import Denied, PrScope
from app.worker.rules.checkers import STALE
from app.worker.rules.judge import CodeReader
from app.worker.rules.judge_pure import reads_from_json
from app.worker.rules.pr_check_pure import PrFacts
from app.worker.rules.pr_description_pure import (
    ANSWER_SCHEMA,
    CALL_BUDGET_USD,
    MAX_TOOL_USES,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    DescriptionUsage,
    PrDescription,
    build_prompt,
    changed_lines,
    described_from,
    description_key,
    is_empty,
    items_from_json,
    items_to_json,
    parse_answer,
    parse_diff,
    pipeline_lines,
    pipeline_text,
    render_view,
    verify,
)
from app.worker.rules.rules import AskModel
from app.worker.state import Store

log = logging.getLogger(__name__)

STAGE: Final = "pr_description"


@dataclass(frozen=True)
class Tools:
    store: Store
    budget: CallBudget
    ask: AskModel  # 구조화 출력 호출(`claude_cli.run_structured`)
    model: str  # `model.judge` (위반 판단과 같은 몫)


def pipeline_diffs(worktree: Path, facts: PrFacts) -> tuple[str, ...]:
    """파이프라인 수정안의 커밋(머지 커밋이 아닌 것)마다 그 커밋이 바꾼 내용. 읽지 못한 커밋은
    빼고 로그에 남긴다(그 변경은 모델이 PR의 변경으로 볼 수 있다)."""
    out = []
    for c in facts.commits:
        if not c.pipeline or c.merge:
            continue
        try:
            out.append(repo.commit_patch(worktree, c.sha))
        except (repo.GitError, UnicodeDecodeError) as e:
            log.warning("파이프라인 수정안 커밋의 변경을 읽지 못함 %s: %s", c.sha[:7], e)
    return tuple(out)


def check(
    facts: PrFacts | str,
    changes_diff: str,
    worktree: Path,
    tools: Tools,
    *,
    pipeline: Sequence[str] = (),
    known: int = 0,
    is_stale: Callable[[], bool] = lambda: False,
) -> PrDescription:
    """PR 글과 diff를 견준다. facts가 글이면 PR 사실을 읽지 못한 까닭이다. pipeline은 파이프라인
    수정안의 커밋들이 바꾼 내용(`pipeline_diffs`), known은 이 검사의 코드 위반 수다(PR 상한의 대상
    위반 수). 설명이 비었으면 모델을 부르지 않는다."""
    if isinstance(facts, str):
        return PrDescription("unchecked", gap="no_facts", detail=facts)
    described = described_from(facts)
    if is_empty(described):
        return PrDescription("empty", text_cut=described.cut)
    files = parse_diff(changes_diff)
    if not files:
        return PrDescription("unchecked", gap="no_diff", text_cut=described.cut)
    view = render_view(files)
    shown_pipeline = pipeline_text(pipeline)
    base = PrDescription("checked", files=view.files, shown=view.shown, text_cut=described.cut)
    changed, by_pipeline = changed_lines(files), pipeline_lines(pipeline)
    key = description_key(described, changes_diff, shown_pipeline, tools.model)
    with ExitStack() as stack:
        code = CodeReader(worktree, tools.ask, stack)
        entry = tools.store.cached_result(key)
        if entry is not None and code.unchanged(reads_from_json(entry.result_json)):
            try:
                saved = items_from_json(entry.result_json)
            except ValueError as e:
                log.warning("저장한 PR 설명 확인의 답을 읽지 못해 다시 물음: %s", e)
            else:
                verified = verify(saved, described, changed, by_pipeline)
                tools.budget.record_hit(PrScope(), stage=STAGE, tier="light", model=tools.model)
                usage = DescriptionUsage(cached=1, dropped=len(verified.dropped))
                return _with(base, items=verified.kept, usage=usage)
        if is_stale():
            return _with(base, status="unchecked", gap="cap", detail=STALE)
        call = tools.budget.with_targets(known).begin(
            PrScope(), stage=STAGE, tier="light", model=tools.model
        )
        if isinstance(call, Denied):
            log.info("PR 설명 확인을 하지 않음: %s", call.reason)
            return _with(base, status="unchecked", gap="cap", detail=call.reason)
        answer = code.ask(
            build_prompt(described, view, shown_pipeline),
            system_prompt=SYSTEM_PROMPT,
            schema=ANSWER_SCHEMA,
            model=tools.model,
            max_budget_usd=CALL_BUDGET_USD,
            max_tool_uses=MAX_TOOL_USES,
        )
        log.info(
            "PR 설명 확인 호출: 도구 %d회, 읽은 경로 %s, 입력 %s, 출력 %s토큰, 거부 %d건",
            answer.tool_uses, list(answer.reads), answer.input_tokens, answer.output_tokens,
            answer.permission_denials,
        )  # fmt: skip
        failure = None if answer.ok else f"{answer.error}: {answer.detail}"
        items, reads = None, {}
        if failure is None:
            try:
                items = parse_answer(answer.data or {})
                reads = code.digests(answer.reads)
            except ValueError as e:
                failure = f"답의 형식이 틀림: {str(e).splitlines()[0]}"
            except repo.GitError as e:
                failure = f"모델이 읽은 파일을 확인하지 못함: {e}"
        tools.budget.finish(
            call, ok=failure is None, error=failure, model_id=answer.model_id,
            input_tokens=answer.input_tokens, output_tokens=answer.output_tokens,
            permission_denials=answer.permission_denials,
        )  # fmt: skip
        model_id = answer.model_id or tools.model
        usage = DescriptionUsage(
            calls=1, tool_uses=answer.tool_uses, input_tokens=answer.input_tokens or 0,
            output_tokens=answer.output_tokens or 0, models=(model_id,),
        )  # fmt: skip
        if failure is not None or items is None:
            log.warning("PR 설명 확인 실패: %s", failure)
            return _with(base, status="unchecked", gap="model_failed", detail=failure or "",
                         usage=usage)  # fmt: skip
        verified = verify(items, described, changed, by_pipeline)
        for reason in verified.dropped:
            log.info("PR 설명 확인의 답을 버림: %s", reason)
        tools.store.save_result(key, stage=STAGE, model=model_id, prompt_version=PROMPT_VERSION,
                                result_json=items_to_json(verified.kept, reads))  # fmt: skip
        return _with(base, items=verified.kept, usage=replace(usage, dropped=len(verified.dropped)))


def _with(result: PrDescription, **changes: object) -> PrDescription:
    return replace(result, **changes)  # type: ignore[arg-type]
