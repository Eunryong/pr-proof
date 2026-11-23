"""이슈 확인 (docs/DESIGN.md 4.5 끝 "연결된 이슈 확인", pr-proof#75): PR이 닫겠다고 한 이슈가 요구한
것을 PR의 변경이 해결했는지 모델이 견주고, 빠진 것을 결과 코멘트에 알린다. 고치지 않는다.

그래프의 `linked_issues` node가 PR 설명 확인 다음, 테스트 전에 부른다(규칙이 없어도 돈다).
가드레일(8.6)이 들인 작업에서만 이 node까지 오고, 그때만 이슈를 읽는다.

연결된 이슈는 GitHub의 `closingIssuesReferences`(본문의 `Closes #N`과 PR 화면에서 연결한 것)로
받는다(`GitHub.linked_issues`). 연결된 이슈가 없으면 모델을 부르지 않는다. 모델은 PR 설명 확인과
같은 장치로 부른다: `judge.CodeReader`(읽기 도구만 주는 모델 폴더, 읽은 경로의 해시, 앞 커밋 뒤 바뀐
파일). 호출은 PR 한 번에 하나이고(이슈가 여럿이어도 한 호출에 묶는다) PR 상한(9.2)에서 센다. 답은
(보인 이슈 글, diff, 모델, 프롬프트 판)의 키로 모델이 읽은 경로의 해시와 함께 저장하고, 그 경로가
모두 그대로일 때만 다시 쓴다. 호출 상한, 실패는 저장하지 않는다. PR마다 바로 앞 결과도 한 줄 둔다
(`earlier_key`, PR 설명 확인과 같다).
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
from app.worker.github import GitHub, GitHubError
from app.worker.rules.checkers import STALE
from app.worker.rules.judge import CodeReader
from app.worker.rules.judge_pure import pr_label, reads_from_json
from app.worker.rules.linked_issues_pure import (
    ANSWER_SCHEMA,
    CALL_BUDGET_USD,
    MAX_TOOL_USES,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    IssueGap,
    IssuesEarlier,
    Linked,
    LinkedIssues,
    Shown,
    build_prompt,
    earlier_from_json,
    earlier_key,
    earlier_to_json,
    from_shown,
    issues_key,
    items_from_json,
    items_to_json,
    parse_answer,
    select,
    verify,
)
from app.worker.rules.pr_description_pure import DescriptionUsage, parse_diff, render_view
from app.worker.rules.rules import AskModel
from app.worker.state import Store

log = logging.getLogger(__name__)

STAGE: Final = "linked_issues"


@dataclass(frozen=True)
class Tools:
    store: Store
    budget: CallBudget
    ask: AskModel  # 구조화 출력 호출(`claude_cli.run_structured`)
    model: str  # `model.judge` (PR 설명 확인과 같은 몫)


def read(github: GitHub, repo_name: str, pr_number: int) -> Linked | str:
    """연결된 이슈. 읽지 못하면 그 까닭(글)이다."""
    try:
        return github.linked_issues(repo_name, pr_number)
    except GitHubError as e:
        log.warning("연결된 이슈를 읽지 못함 %s#%s: %s", repo_name, pr_number, e)
        text = str(e).strip()
        return f"연결된 이슈를 읽지 못함: {text.splitlines()[0] if text else e}"


def check(
    linked: Linked | str,
    repo_name: str,
    changes_diff: str,
    worktree: Path,
    tools: Tools,
    *,
    known: int = 0,
    is_stale: Callable[[], bool] = lambda: False,
) -> LinkedIssues:
    """이슈가 요구한 것과 diff를 견준다. linked가 글이면 이슈를 읽지 못한 까닭이다. known은 이
    검사의 코드 위반 수다(PR 상한의 대상 위반 수). 견줄 이슈가 없으면 모델을 부르지 않는다."""
    if isinstance(linked, str):
        return LinkedIssues("unchecked", gap="unreadable", detail=linked)
    shown = select(linked, repo_name)
    if not shown.issues:
        if shown.other_repo:
            return from_shown(shown, "unchecked", gap="other_repo")
        if shown.unreadable:
            return from_shown(shown, "unchecked", gap="unreadable",
                              detail="연결된 이슈를 읽을 권한이 없거나 지워짐")  # fmt: skip
        return from_shown(shown, "none")
    files = parse_diff(changes_diff)
    if not files:
        return from_shown(shown, "unchecked", gap="no_diff")
    view = render_view(files)
    base = from_shown(shown, files=view.files, shown=view.shown)
    key = issues_key(shown, changes_diff, tools.model)
    head = tools.budget.head_sha
    pointer = earlier_key(pr_label(tools.budget.repo, tools.budget.pr_number), tools.model)
    with ExitStack() as stack:
        code = CodeReader(worktree, tools.ask, stack)
        entry = tools.store.cached_result(key)
        if entry is not None and code.unchanged(reads_from_json(entry.result_json)):
            try:
                saved = items_from_json(entry.result_json)
            except ValueError as e:
                log.warning("저장한 이슈 확인의 답을 읽지 못해 다시 물음: %s", e)
            else:
                verified = verify(saved, shown)
                tools.budget.record_hit(PrScope(), stage=STAGE, tier="light", model=tools.model)
                _remember(tools.store, pointer, verified.kept, head, shown, tools.model)
                usage = DescriptionUsage(cached=1, dropped=len(verified.dropped))
                return replace(base, items=verified.kept, usage=usage)
        if is_stale():
            return replace(base, status="unchecked", gap="cap", detail=STALE)
        call = tools.budget.with_targets(known).begin(
            PrScope(), stage=STAGE, tier="light", model=tools.model
        )
        if isinstance(call, Denied):
            log.info("이슈 확인을 하지 않음: %s", call.reason)
            return replace(base, status="unchecked", gap="cap", detail=call.reason)
        earlier = _earlier(tools.store, pointer, shown, code, head)
        answer = code.ask(
            build_prompt(shown, view, earlier),
            system_prompt=SYSTEM_PROMPT,
            schema=ANSWER_SCHEMA,
            model=tools.model,
            max_budget_usd=CALL_BUDGET_USD,
            max_tool_uses=MAX_TOOL_USES,
        )
        log.info(
            "이슈 확인 호출: 이슈 %s, 도구 %d회, 읽은 경로 %s, 입력 %s, 출력 %s토큰, 거부 %d건",
            list(shown.numbers), answer.tool_uses, list(answer.reads), answer.input_tokens,
            answer.output_tokens, answer.permission_denials,
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
            earlier=int(earlier is not None), earlier_items=len(earlier.items) if earlier else 0,
        )  # fmt: skip
        if failure is not None or items is None:
            log.warning("이슈 확인 실패: %s", failure)
            return replace(base, status="unchecked", gap="model_failed", detail=failure or "",
                           usage=usage)  # fmt: skip
        verified = verify(items, shown, earlier.items if earlier else ())
        for reason in verified.dropped:
            log.info("이슈 확인의 답을 버림: %s", reason)
        if earlier is not None:
            log.info("이슈 확인: 지난 결과를 주고 다시 견줌, 지난 항목 %d건 중 유지 %d건",
                     len(earlier.items), verified.same)  # fmt: skip
        tools.store.save_result(key, stage=STAGE, model=model_id, prompt_version=PROMPT_VERSION,
                                result_json=items_to_json(verified.kept, reads))  # fmt: skip
        _remember(tools.store, pointer, verified.kept, head, shown, model_id)
        usage = replace(usage, dropped=len(verified.dropped), kept_earlier=verified.same)
        return replace(base, items=verified.kept, usage=usage)


def _earlier(
    store: Store, pointer: str, shown: Shown, code: CodeReader, head: str
) -> IssuesEarlier | None:
    """같은 PR의 바로 앞 결과와 그 뒤 바뀐 파일. 없거나 읽지 못하면 None이다."""
    entry = store.cached_result(pointer)
    if entry is None:
        return None
    try:
        earlier = earlier_from_json(entry.result_json, shown)
    except ValueError as e:
        log.warning("이슈 확인의 지난 결과를 읽지 못함: %s", e)
        return None
    return replace(earlier, changed=code.changed_since(earlier.sha, head))


def _remember(
    store: Store, pointer: str, items: Sequence[IssueGap], head: str, shown: Shown, model: str
) -> None:
    """이 커밋의 결과를 이 PR의 바로 앞 결과로 둔다 (다음 커밋이 읽는다)."""
    store.save_result(pointer, stage=f"{STAGE}_earlier", model=model,
                      prompt_version=PROMPT_VERSION,
                      result_json=earlier_to_json(items, head, shown))  # fmt: skip
