"""결과 코멘트의 규칙 줄과 규칙 절 (docs/DESIGN.md 4.9, 9.4). 판정을 새로 내지 않는다.

`DocRules` 값 하나와, 위반을 찾은 검사기의 본문(`checkers` 테이블에서 읽은 것), 위반마다의 수정
판단(`Decided`), 묶음마다의 수정 상태(`FixView`)만 받는다. 규칙의 인용, 문서의 절 이름, 파일 경로,
도구의 메시지, 검사기 본문과 예시, 실패 이유, 모델이 쓴 수정 판단의 이유는 모두 믿지 않는 글이라
코드 스팬이나 코드 울타리 안에만 넣는다 (app/worker/core/markdown_pure.py).
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final, Literal, get_args

from app.worker.core.bundle_pure import MECHANICAL_BUNDLE
from app.worker.core.checker_pure import TOOL_KINDS, Checker
from app.worker.core.markdown_pure import code_fence, inline_code
from app.worker.core.rule_pure import Rule
from app.worker.core.violation_pure import Violation
from app.worker.fix.fix_decision_pure import Decided, FixDecision, SkipReason
from app.worker.fix.fix_pure import MechanicalFix
from app.worker.fix.model_fix_pure import ModelFix
from app.worker.fix.proposal_pure import ProposalResult
from app.worker.report import pr_check_report_pure
from app.worker.rules.checker_authoring_pure import JudgeRoute
from app.worker.rules.doc_rules_pure import (
    CallUsage,
    DocRules,
    Found,
    NoRuleDocs,
    NoRulesFound,
    NotRun,
    RuleChecked,
    RuleJudged,
    RuleOutcome,
    RuleUnchecked,
    outcomes_by_rule,
)
from app.worker.rules.pr_check_pure import PrChecks
from app.worker.rules.rule_docs_source_pure import DocsSource, RuleChange
from app.worker.store.rag_pure import KIND_LABEL, SITE_LABEL, Retrieval, merge_retrievals
from app.worker.store.rag_switch_pure import RagSwitch, comment_line
from app.worker.testing.test_lines import generated_lines, runs_without_new_tests, status_lines

TITLE: Final = "**규칙:**"
SOURCE_TITLE: Final = "**규칙 문서:**"
# 규칙 문서를 고친 PR의 줄에 적는 문서 수와 접는 블록에 한쪽마다 싣는 규칙 수
MAX_LISTED_DOCS: Final = 5
MAX_LISTED_CHANGES: Final = 30
VIOLATION: Final = "⚠️"
CLEAN: Final = "✅"
NO_RULES: Final = "규칙을 찾지 못해 규칙 검사를 하지 않음"
HOW_TO_WRITE: Final = (
    "이 저장소의 `CONTRIBUTING.md`에 팀 규칙을 적어 두면 다음 PR부터 그 규칙으로 검사합니다. "
    "`## 코드 규칙` 같은 절 아래에 한 문장씩 적으면 됩니다. "
    '예: "`orders/` 아래 모듈은 `print`를 쓰지 않는다."\n'
    "\n"
    "- `README.md`에 적어도 읽습니다. 다른 문서에 적었으면 base 브랜치 `.pipeline.yaml`의 "
    "`rules.docs`에 그 경로를 적습니다.\n"
    "- ruff 설정이 이미 있는 저장소는 그 설정도 함께 검사합니다."
)
# 규칙마다 바로 보이는 위반 수. 나머지는 접는다 (11.2 D6)
MAX_SHOWN_PER_RULE: Final = 10
# 접은 목록에 싣는 위반 수의 합. 넘는 것은 건수만 적는다 (코멘트 길이)
MAX_FOLDED_VIOLATIONS: Final = 300
MANY_VIOLATIONS_NOTE: Final = (
    "위반이 많습니다. 규칙이 문서에 새로 추가돼 기존 코드의 위반이 한꺼번에 나온 것일 수 있습니다. "
    "규칙마다 고칠 대상은 앞 10건만 보이고, 나머지와 직접 고쳐야 하는 위반의 위치는 접었습니다."
)
MAX_LISTED_RULES: Final = 60
MAX_CHECKER_DETAILS: Final = 10
MAX_REFERENCES: Final = 30
MAX_LISTED_SECTIONS: Final = 80
SECTIONS_NOTE: Final = (
    "읽지 않은 절의 규칙은 이 PR에 적용되지 않았습니다. `.pipeline.yaml`의 `rules.docs`를 규칙이 "
    "있는 문서로 좁혀 합계가 400KB 이하가 되면 문서 전체를 읽습니다."
)
MAX_BODY_CHARS: Final = 4000
MAX_QUOTE_CHARS: Final = 500
MAX_DETAIL_CHARS: Final = 300
# 절 전체의 상한. GitHub 코멘트 한도(65536자)에서 v1 절(패치 6000자 둘 포함)을 빼고도 남게
MAX_SECTION_CHARS: Final = 30_000
OMITTED: Final = "… (코멘트 길이 제한으로 나머지를 생략)"

DETAILS_SUMMARY: Final = "자세히: 규칙별 확인 방법, 모델 호출, 바뀐 줄 커버리지"
_UNCHECKED_TEXT: Final[Mapping[str, str]] = {
    "pr": "PR 단위 규칙 (PR 단위 확인으로 봄)",
    "no_checker": "읽었지만 아직 검사하지 못함",
    "cap": "호출 상한에 걸려 이번에 처리하지 못함",
    "model_failed": "검사하지 못함 (모델 호출 실패)",
    "tool_unavailable": "검사하지 못함 (도구 준비 실패)",
    "run_failed": "검사하지 못함 (검사기 실행 실패)",
}
# 체크포인트에 남은 옛 결과의 종류(2026-11-22 전의 "human" 등)는 이 표에 없다. 이번 검사가 확인한
# 것이 아니므로 확인하지 못한 규칙으로 보이고, `/recheck`가 새 규칙 읽기로 다시 확인한다
OLD_KIND_TEXT: Final = "이전 검사의 결과라 이번에 확인하지 못함. `/recheck`로 다시 확인합니다"
_FAILED_KINDS: Final = frozenset({"model_failed", "tool_unavailable", "run_failed"})
_CONFIRMED_LABEL: Final[Mapping[str, str]] = {
    "tool": "도구·검사기", "model": "모델 판단", "run": "테스트·정적 검사 결과", "pr": "PR 단위",
}  # fmt: skip
# 모델 판단으로 보낸 까닭 (4.4 "의미 규칙을 모델 판단으로 보내는 때"). none·part 뒤에는 모델이 적은
# "검사기로 가릴 수 없는 부분"을 붙인다
_JUDGE_ROUTE_TEXT: Final[Mapping[JudgeRoute, str]] = {
    "none": "검사기로 가릴 수 없는 규칙이라고 검사기 작성 모델이 답함",
    "part": "검사기로는 일부만 가릴 수 있다고 검사기 작성 모델이 답함",
    "examples": "검사기가 예시 확인을 두 번 통과하지 못함",
    "attempts": "예시를 통과하는 검사기를 만들지 못함",
}
# 수정 판단이 도구 설정의 규칙 중 도구의 수정이 없는 위반에 적는 까닭 글 (`fix_decision_pure`)
NO_AUTOFIX_WHY: Final = "도구의 자동 수정이 없음"
# 고치지 않은 위반의 이유 (4.9): (짧은 이름, 문장). 문장은 규칙마다 한 번 "직접 고쳐야 함 N건" 옆에
# 적고, 짧은 이름은 한 규칙에 이유가 여럿일 때 위치마다와 "수정 판단:" 줄의 이유별 건수에 쓴다.
# 알리기만 하는 규칙(notify_only)은 위반의 모양으로 나눈다(`_left_reason`). "알리기만 하는
# 규칙"이라는 말은 쓰지 않는다: 위반마다 붙어 읽기 어려웠고 왜 고치지 않았는지를 말하지
# 않았다 (2026-11-23)
_LEFT_REASON: Final[Mapping[str, tuple[str, str]]] = {
    "rejected_before": (
        "이전에 거절됨",
        "같은 줄을 고친 수정안이 머지되지 않고 닫혀 다시 만들지 않았습니다.",
    ),
    "notify_file": (
        "파일 이름·위치를 바꿔야 함",
        "파일 이름이나 위치를 바꿔야 하는 위반이라 수정안을 만들지 않았습니다.",
    ),
    "notify_tool": (
        NO_AUTOFIX_WHY,
        "도구에 이 위반의 자동 수정이 없어 수정안을 만들지 않았습니다.",
    ),
    # 규칙 읽기의 수정 방법 `none` (4.4 "수정 방법", rule_reading_pure의 fix 설명)
    "notify_rule": (
        "그 파일 안에서 고칠 수 없는 규칙",
        "규칙을 읽은 모델이 그 파일 안에서는 고칠 수 없는 규칙(다른 파일의 사용처까지 바꿔야 "
        "하거나 코드로 고칠 수 없음)이라고 답해 수정안을 만들지 않았습니다.",
    ),
    "no_tests": (
        "테스트로 확인할 수 없음",
        "테스트가 없어 고친 것을 확인할 수 없으므로 수정안을 만들지 않았습니다.",
    ),
    "flaky": (
        "테스트로 확인할 수 없음",
        "불안정한 테스트가 있어 고친 것을 확인할 수 없으므로 수정안을 만들지 않았습니다.",
    ),
    "team_pattern": (
        "팀이 닫은 수정과 비슷함",
        "팀이 전에 닫은 수정안과 비슷하다고 모델이 보아 수정안을 만들지 않았습니다.",
    ),
    "needs_other_file": (
        "다른 파일이나 공개 이름을 바꿔야 함",
        "고치려면 그 파일 밖의 다른 파일이나 다른 파일이 쓰는 이름을 바꿔야 한다고 모델이 보아 "
        "수정안을 만들지 않았습니다.",
    ),
    "changes_behavior": (
        "고치면 동작이 바뀜",
        "규칙을 지키는 어떤 변경도 동작을 바꾼다고 모델이 보아 수정안을 만들지 않았습니다.",
    ),
    "not_a_violation": (
        "모델이 위반이 아니라고 봄",
        "모델이 위반이 아니라고 보아 수정안을 만들지 않았습니다.",
    ),
    "risky": ("모델이 고치지 않기로 함", "모델이 고치지 않기로 해 수정안을 만들지 않았습니다."),
    "call_limit": (
        "호출 상한",
        "모델 호출 상한에 걸려 고칠지 판단하지 못했습니다. `/fix`로 다시 돌릴 수 있습니다.",
    ),
    "undecided": (
        "수정 판단을 하지 못함",
        "모델이 고칠지 판단하지 못해 수정안을 만들지 않았습니다.",
    ),
}
# 수정 판단을 하지 못한 위반(undecided)은 파이프라인이 적은 까닭으로 나눈다 (`fix/decide.py`의
# CALL_FAILED, `fix_decision_pure.reconcile`)
_UNDECIDED: Final[Mapping[str, tuple[str, str]]] = {
    "모델이 답하지 않음": (
        "모델이 답하지 않음",
        "모델이 이 위반에 답하지 않아 수정안을 만들지 않았습니다.",
    ),
    "수정 판단을 하지 못함 (모델 호출 실패)": (
        "모델 호출 실패",
        "모델 호출이 실패해 고칠지 판단하지 못했습니다.",
    ),
}
# 테스트가 통과하지 않은 위반(tests_failed)의 문장 앞부분. 키는 수정 판단이 적은 까닭이다
# (`fix_decision_pure._TESTS_BROKEN`)
_TESTS_BROKEN_HEAD: Final[Mapping[str, str]] = {
    "테스트가 실패함": "테스트가 실패해",
    "테스트가 시간 안에 끝나지 않음": "테스트가 시간 안에 끝나지 않아",
    "테스트를 돌리지 못함": "테스트를 돌리지 못해",
    "테스트가 중단됨": "테스트가 중단돼",
}
_SKIP_REASONS: Final = frozenset(get_args(SkipReason))
LEFT: Final = "직접 고쳐야 함"
# "무엇이 문제인가" 줄: 그 규칙의 위반들이 같은 메시지를 가질 때 규칙 블록에 한 번 싣는다
PROBLEM: Final = "무엇이 문제인가:"
_FIX_FAILED: Final[Mapping[str, str]] = {
    "failed": "정해진 회차 안에 검증을 통과하지 못함",
    "dropped": "수정안을 버림",
    "refused": "모델을 실행하지 않음",
    "nothing_fixed": "적용할 수 있는 수정이 없음",
    "new_violations": "수정안을 버림: 고친 뒤 원래 없던 위반이 생김",
    "out_of_scope": "수정안을 버림: 허용 범위 밖의 파일이 바뀜",
    "secret": "수정안을 버림: 비밀값으로 보이는 내용이 있음",
    "tests_changed": "수정안을 버림: 고친 뒤 테스트 결과가 달라짐",
    "unchecked": "수정안을 버림: 고친 뒤 규칙 재검사를 하지 못함",
    "too_big": "수정안을 버림: 크기 상한을 넘음",
}
_LABEL: Final[Mapping[str, str]] = {MECHANICAL_BUNDLE: "기계적 수정안", "model": "모델 수정안"}
MAX_PATCH_CHARS: Final = 6000
# `/fix`는 PR 호출 상한을 한 번 더 받아 남은 수정 판단과 수정을 이어 한다 (4.9, 9.2)
RERUN_HINT: Final = "`/fix`로 다시 돌릴 수 있습니다."
FixState = Literal["running", "approval", "merged", "open", "rejected", "done"]


@dataclass(frozen=True)
class FixView:
    """모델 수정 묶음의 지금 상태 (4.9 "수정 결과"). 그래프가 만들고 코멘트는 그리기만 한다."""

    targets: int  # 그 묶음의 수정 대상 위반 수
    size_capped: int  # 크기 상한으로 수정안에 넣지 못한 위반 수 (11.2 D6)
    state: FixState
    number: int | None = None  # merged·open·rejected의 수정안 PR 번호
    fix: ModelFix | MechanicalFix | None = None  # state가 done일 때
    proposal: ProposalResult | None = None
    patch: str = ""  # 검증을 통과한 패치 (`fix_attempts`). 수정안 PR을 열지 못했을 때 보인다


@dataclass(frozen=True)
class RunAnswer:
    """run 규칙 하나의 답 (4.5). verdict는 모델의 판단이고, unknown은 판단하지 못했거나 묻지 못한
    것이다. basis는 모델에게 준 실행 결과의 요약, why는 모델의 까닭이나 묻지 못한 이유다."""

    rule: Rule
    verdict: Literal["kept", "broken", "unknown"]
    basis: str
    why: str


def run_answers(found: Found) -> dict[str, RunAnswer]:
    """run 규칙마다 테스트 뒤 모델이 정한 답(`Found.runs`). 테스트 전에 끝났으면 묻지 못한
    것이다."""
    judged = {r.rule_id: r for r in found.runs}
    out: dict[str, RunAnswer] = {}
    for o in _unchecked(found.outcomes, frozenset({"run"})):
        r = judged.get(o.rule.id)
        if r is None:
            out[o.rule.id] = RunAnswer(o.rule, "unknown", "", "테스트를 돌리기 전에 끝남")
            continue
        verdict = r.verdict if r.verdict in ("kept", "broken") else "unknown"
        out[o.rule.id] = RunAnswer(o.rule, verdict, r.facts, r.why)  # type: ignore[arg-type]
    return out


def dismissed(found: Found, decisions: Decided | None) -> tuple[Found, list[tuple[Violation, str]]]:
    """도구·검사기가 잡았지만 수정 판단의 모델이 위반이 아니라고 답한 것을 뺀 결과와, 뺀 위반과 그
    까닭. 뺀 것은 위반 수에도 "직접 고쳐야 함"에도 들지 않고 접는 블록에 남는다 (4.6, 4.9)."""
    if decisions is None:
        return found, []
    out = {d.violation_id: d.why for d in decisions.decisions
           if d.reason == "not_a_violation" and d.decided_by == "model"}  # fmt: skip
    if not out:
        return found, []
    kept = tuple(v for v in found.violations if v.id not in out)
    return replace(found, violations=kept), [(v, out[v.id]) for v in found.violations
                                             if v.id in out]  # fmt: skip


def has_violations(result: DocRules | None, decisions: Decided | None = None) -> bool:
    """위반 수에 드는 코드 위반이 있다 (모델이 위반이 아니라고 본 것은 빼고). 테스트가 돌지 않았을
    때의 안내는 이때만 붙인다 (report.py)."""
    if not isinstance(result, Found):
        return False
    return bool(dismissed(result, decisions)[0].violations)


def title(
    result: DocRules | None,
    fixes: Mapping[str, FixView] | None = None,
    pr: PrChecks | None = None,
    decisions: Decided | None = None,
) -> str | None:
    """코멘트 맨 위의 "규칙:" 줄, 결론이다 (4.9). 규칙 경로를 돌기 전에 끝났으면 None. pr은 PR 단위
    확인의 결과다(PR 규칙이 있는 검사만). PR 단위 위반이 있으면 그것은 따로 된 줄
    (`pr_check_report_pure.title`)이 말하고 이 줄은 코드와 실행 결과의 규칙만 말한다."""
    match result:
        case None:
            return None
        case NotRun(reason=reason):
            return f"{TITLE} 검사하지 못함 ({inline_code(reason, MAX_DETAIL_CHARS)})"
        case NoRuleDocs() | NoRulesFound():
            return f"{TITLE} {NO_RULES}"
        case Found():
            numbers = [n for v in (fixes or {}).values() if (n := _proposal_number(v)) is not None]
            counted, _ = dismissed(result, decisions)
            conclusion = _title_line(counted, pr, run_answers(result))
            if conclusion is None:
                return None
            line = f"{TITLE} {conclusion}"
            return line + (f" · 수정안 {', '.join(f'#{n}' for n in numbers)}" if numbers else "")


def source_line(result: DocRules | None) -> str | None:
    """ "규칙:" 줄 아래의 "규칙 문서:" 줄. PR이 규칙 문서를 고쳤을 때만 있다 (4.3 "규칙 문서의
    원천"). 어느 쪽 문서로 읽은 규칙으로 검사했는지 말한다."""
    source = result.source if result is not None else None
    if source is None:
        return None
    docs = [inline_code(p, 200) for p in source.changed[:MAX_LISTED_DOCS]]
    if len(source.changed) > MAX_LISTED_DOCS:
        docs.append(f"외 {len(source.changed) - MAX_LISTED_DOCS}개")
    line = f"{SOURCE_TITLE} 이 PR이 규칙 문서를 바꿉니다 ({', '.join(docs)})."
    # 규칙 경로를 끝까지 돌지 못했거나 규칙 문서를 읽지 못했으면 그 규칙으로 검사했다고 적지 않는다
    done = not isinstance(result, NotRun) and not (isinstance(result, Found) and result.note)
    if source.read_from == "base":
        how = "했습니다" if done else "하려 했습니다"
        return f"{line} 이번 검사는 base의 규칙으로 {how}. 머지된 뒤부터 새 규칙으로 검사합니다."
    line += " 이 PR의 문서로 읽은 규칙으로 " + ("검사했습니다." if done else "검사하려 했습니다.")
    if source.compare_skipped:
        why = inline_code(source.compare_skipped, MAX_DETAIL_CHARS)
        line += f" base의 규칙과는 견주지 못했습니다 ({why})."
    return line


def source_changes(source: DocsSource | None) -> list[str]:
    """PR의 문서로 읽었을 때 없어지는 규칙과 새로 생기는 규칙의 접는 블록. 없으면 줄도 없다. 규칙의
    원문은 PR이 쓴 글이라 코드 스팬 안에만 넣는다."""
    if source is None or not (source.removed or source.added):
        return []
    summary = f"없어지는 규칙 {len(source.removed)}개, 새로 생기는 규칙 {len(source.added)}개"
    lines = ["", f"<details><summary>{summary}</summary>"]
    for label, changes in (("없어지는 규칙", source.removed), ("새로 생기는 규칙", source.added)):
        if changes:
            lines += ["", f"{label}:", *_change_lines(changes)]
    return [*lines, "", "</details>"]


def _change_lines(changes: Sequence[RuleChange]) -> list[str]:
    lines = [
        f"- `{c.id}` {inline_code(c.text, MAX_QUOTE_CHARS)}" for c in changes[:MAX_LISTED_CHANGES]
    ]
    if len(changes) > MAX_LISTED_CHANGES:
        lines.append(f"- 외 {len(changes) - MAX_LISTED_CHANGES}개")
    return lines


def _proposal_number(view: FixView) -> int | None:
    """이 커밋의 그 묶음 수정안 PR 번호 (이번에 열었거나 이미 열려 있음)."""
    if view.proposal is not None and view.proposal.number is not None:
        return view.proposal.number
    return view.number if view.state == "open" else None


def section(
    result: DocRules | None,
    checkers: Mapping[str, Checker],
    decisions: Decided | None = None,
    fixes: Mapping[str, FixView] | None = None,
    rag: RagSwitch | None = None,
    pr: PrChecks | None = None,
    more: Sequence[str] = (),
) -> list[str]:
    """규칙 절의 줄들 ("규칙:" 줄 아래). checkers는 위반을 찾은 검사기의 {CheckerRef.key: 검사기}다.

    decisions는 테스트 뒤의 수정 판단이다. 아직 없으면(테스트 전에 끝남) 판단 없이 그린다.
    fixes는 묶음(mechanical, model)마다의 수정 상태다. 그 묶음으로 정한 위반이 없으면 없다.
    rag는 이 검사의 검색 상태다. more는 부르는 쪽이 접는
    블록에 더할 줄(테스트 수, 바뀐 줄 커버리지)이다.

    위반과 확인하지 못한 규칙, 할 일은 펼친 채로 두고, 규칙별 확인 방법과 사용량은 접는 블록 하나에
    넣는다. 위반이 없으면 펼친 부분이 거의 없다 (4.9).
    """
    changes = source_changes(result.source) if result is not None else []
    match result:
        case None | NotRun():
            return changes + _details(list(more))
        case NoRuleDocs(unread=unread):
            lines = [*changes, "", HOW_TO_WRITE]
            if unread:
                lines += ["", "읽지 못한 규칙 문서:"]
                lines += [f"- {inline_code(d.path)}: {inline_code(d.reason)}" for d in unread]
            return lines + _details(list(more))
        case NoRulesFound():
            return [*changes, "", HOW_TO_WRITE, *_details(list(more))]
        case Found():
            return _found(result, checkers, decisions, fixes or {}, rag, pr, more)


def _found(
    found: Found,
    checkers: Mapping[str, Checker],
    decisions: Decided | None,
    fixes: Mapping[str, FixView],
    rag: RagSwitch | None = None,
    pr: PrChecks | None = None,
    more: Sequence[str] = (),
) -> list[str]:
    found, set_aside = dismissed(found, decisions)
    head = source_changes(found.source)
    head += (
        ["", f"규칙 문서를 읽지 못해 도구 설정의 규칙만 검사함 "
         f"({inline_code(found.note, MAX_DETAIL_CHARS)})"]
        if found.note else []
    )  # fmt: skip
    head += sections_read(found.sections)
    answers = run_answers(found)
    flagged = {v.rule_id for v in found.violations}
    by_id = {d.violation_id: d for d in decisions.decisions} if decisions else {}
    shown = [
        _violations(found, by_id, fixed_in(fixes)),
        _run_broken(answers),
        _decision_summary(decisions, found.violations),
        *(_fix_lines(view, bundle, _test_lines(found, view)) for bundle, view in fixes.items()),
        pr_check_report_pure.violations(pr) if pr is not None else [],
        _unresolved(found, answers, flagged),
        _judge_gaps(found),
        _not_checkable(found.outcomes),
        _failed(found.outcomes),
        _capped(found.outcomes),
        pr_check_report_pure.unresolved(pr) if pr is not None else [],
        _other_targets(found.outcomes, pr_checked=pr is not None),
        _checker_details(found, checkers),
    ]
    folded = [
        _dismissed_lines(set_aside),
        _judge_usage(found),
        ["", usage_line(found.usage)],
        pr_check_report_pure.details(pr) if pr is not None else [],
        [f"`rules.disabled`로 끈 규칙 {found.disabled}개"] if found.disabled else [],
        references_block(_retrievals(found, decisions, fixes), rag),
        list(more),
        _read_rules(found, answers),
    ]
    budget = MAX_SECTION_CHARS - _size(head) - _size(_details(["x"]))
    visible = _fit(shown, budget)
    return head + visible + _details(_fit(folded, budget - _size(visible)))


def _details(lines: list[str]) -> list[str]:
    """접는 블록 하나. 줄이 없으면 블록도 없다."""
    if not lines:
        return []
    return ["", f"<details><summary>{DETAILS_SUMMARY}</summary>", *lines, "", "</details>"]


def _undecided(o: RuleOutcome, flagged: set[str]) -> bool:
    """모델이 이 규칙을 판단하지 못했다고 답했고 다른 hunk에서도 위반을 찾지 않았다."""
    return isinstance(o, RuleJudged) and o.undecided > 0 and o.rule.id not in flagged


def _confirmed(
    found: Found, pr: PrChecks | None, answers: Mapping[str, RunAnswer], flagged: set[str]
) -> Counter[str]:
    """확인한 규칙 수를 확인 방법마다 센다. 판단하지 못했거나 이번에 다 보지 못한 규칙은 세지
    않는다. pr이 None이면 PR 단위 규칙은 세지 않는다."""
    counts: Counter[str] = Counter()
    seen = ("checked", "not_applicable")
    pr_seen = {r.rule.id for r in pr.results if r.status in seen} if pr else set()
    for o in found.outcomes:
        if isinstance(o, RuleChecked):
            counts["tool"] += 1
        elif isinstance(o, RuleJudged) and not o.left and not _undecided(o, flagged):
            counts["model"] += 1
        elif o.rule.id in answers and answers[o.rule.id].verdict != "unknown":
            counts["run"] += 1
        elif isinstance(o, RuleUnchecked) and o.kind == "pr" and o.rule.id in pr_seen:
            counts["pr"] += 1
    return counts


def _title_line(
    found: Found, pr_checks: PrChecks | None, answers: Mapping[str, RunAnswer]
) -> str | None:
    """결론. 위반이 있으면 건수만, 없으면 몇 개의 규칙을 무엇으로 확인했는지 (4.9). 규칙이 모두 PR
    단위 규칙이고 그 위반이 따로 된 줄에 나오면 None이다."""
    broken = [a for a in answers.values() if a.verdict == "broken"]
    if found.violations or broken:
        rules = len({v.rule_id for v in found.violations} | {a.rule.id for a in broken})
        return f"{VIOLATION} 위반 {len(found.violations) + len(broken)}건 (규칙 {rules}개)"
    pr_apart = pr_checks is not None and bool(pr_checks.violations)
    flagged = {v.rule_id for v in found.violations}
    counts = _confirmed(found, None if pr_apart else pr_checks, answers, flagged)
    pr_rules = len(_unchecked(found.outcomes, frozenset({"pr"}))) if pr_apart else 0
    total, confirmed = len(found.outcomes) - pr_rules, sum(counts.values())
    if pr_apart and not total:
        return None
    if not confirmed:
        return f"확인한 규칙 없음 (읽은 규칙 {total}개)"
    how = ", ".join(f"{label} {counts[k]}개" for k, label in _CONFIRMED_LABEL.items() if counts[k])
    which = f"규칙 {total}개를" if confirmed == total else f"규칙 {total}개 중 {confirmed}개를"
    return f"{CLEAN} {'코드 ' if pr_apart else ''}위반 없음 · {which} 확인함 ({how})"


def usage_line(usage: CallUsage) -> str:
    """이번 검사의 모델 호출 (9.4). 달러는 쓰지 않는다 (구독 로그인)."""
    line = f"모델 호출 {usage.calls}회 / 상한 {usage.limit}회 (규칙 읽기와 검사기 작성)"
    if usage.models:
        line += f" · {', '.join(inline_code(m, 80) for m in usage.models)}"
    line += f" · 저장된 결과 {usage.cache_hits}건 사용"
    if usage.calls:
        line += f" · 토큰 입력 {usage.input_tokens}, 출력 {usage.output_tokens}"
    return line


def _source(rule: Rule) -> str:
    source = rule.source
    where = f"{source.file} > {source.section}" if source.section else source.file
    return inline_code(where, 200)


def _quote(rule: Rule) -> str:
    return inline_code(rule.text, MAX_QUOTE_CHARS)


def _how_checked(outcome: RuleOutcome, answers: Mapping[str, RunAnswer] | None = None) -> str:
    if isinstance(outcome, RuleChecked):
        ref = outcome.checker
        if outcome.rule.origin == "tool_config":
            return f"도구 `ruff` (`{outcome.rule.source.file}`의 설정)"
        if ref.kind in TOOL_KINDS:
            codes = f" {', '.join(f'`{c}`' for c in ref.codes)}" if ref.codes else ""
            return f"도구 `{ref.kind}`{codes} (모델이 도구 목록에서 고르고 예시로 확인함)"
        saved = ", 저장된 것" if ref.cached else ""
        return f"모델이 쓴 검사기 ({ref.kind}{saved})"
    if isinstance(outcome, RuleJudged):
        why = _JUDGE_ROUTE_TEXT[outcome.route]
        if outcome.route in ("none", "part") and outcome.reason:
            why += f": {inline_code(outcome.reason, MAX_DETAIL_CHARS)}"
        left = f", 이번에 판단하지 못한 바뀐 곳 {outcome.left}군데" if outcome.left else ""
        if outcome.undecided:
            left += f", 모델이 판단하지 못한 바뀐 곳 {outcome.undecided}군데"
        return f"모델 판단 ({why}{left})"
    answer = (answers or {}).get(outcome.rule.id)
    if answer is not None:
        verdict = {"kept": "지킴", "broken": "어김", "unknown": "판단하지 못함"}[answer.verdict]
        basis = f" · 근거: {answer.basis}" if answer.basis else ""
        return f"모델이 테스트·정적 검사 결과로 판단함: {verdict}{basis}"
    return _UNCHECKED_TEXT.get(outcome.kind, OLD_KIND_TEXT)


def _judge_usage(found: Found) -> list[str]:
    """모델 판단의 규칙 수와 호출 (9.4). 접는 블록에 들어간다."""
    usage = found.judge
    if usage is None or not any(isinstance(o, RuleJudged) for o in found.outcomes):
        return []
    line = (
        f"모델 판단: 규칙 {usage.rules}개, 바뀐 곳 {usage.hunks}군데 · 모델 호출 {usage.calls}회 · "
        f"저장된 판단 {usage.cached}건 사용"
    )
    if usage.models:
        line += f" · {', '.join(inline_code(m, 80) for m in usage.models)}"
    if usage.dropped:
        line += f" · 버린 답 {usage.dropped}건 (PR이 바꾼 줄을 그대로 인용하지 않음)"
    if usage.calls:
        line += f" · 토큰 입력 {usage.input_tokens}, 출력 {usage.output_tokens}"
    if not usage.reasked:
        return ["", line]
    # 읽은 곳이 바뀐 저장된 쌍을 지난 판단과 함께 다시 물은 수 (4.5)
    reasked = (
        f"모델 판단: 지난 판단을 주고 다시 물음 {usage.reasked}쌍, 그중 유지 {usage.kept_prior}쌍"
    )
    return ["", line, "", reasked]


def _judge_gaps(found: Found) -> list[str]:
    """모델 판단을 이번에 다 하지 못한 규칙: 호출 상한, 모델 호출 실패 (4.9의 호출 상한 줄)."""
    judged = [o for o in found.outcomes if isinstance(o, RuleJudged)]
    lines: list[str] = []
    capped = [o for o in judged if o.left_kind == "cap"]
    if capped:
        why = inline_code(capped[0].left_detail, MAX_DETAIL_CHARS)
        lines += ["", f"모델 호출 상한에 걸려 처리하지 못함: 위반 판단 {len(capped)}개 규칙 "
                  f"(바뀐 곳 {sum(o.left for o in capped)}군데, {why}). {RERUN_HINT}"]  # fmt: skip
    failed = [o for o in judged if o.left_kind == "model_failed"]
    if failed:
        lines += ["", f"모델 판단을 하지 못한 규칙 {len(failed)}개 (모델 호출 실패. 다음 검사가 "
                  "다시 묻습니다):"]  # fmt: skip
        for o in failed:
            why = inline_code(o.left_detail, MAX_DETAIL_CHARS)
            lines.append(f"- `{o.rule.id}` {_quote(o.rule)} · {why}")
    return lines


def _decision_summary(decisions: Decided | None, violations: Sequence[Violation] = ()) -> list[str]:
    """고칠 대상과 직접 고쳐야 하는 위반의 건수, 수정 판단의 모델 호출 (4.9). 이유가 여럿이면
    이유별 건수를 붙인다(하나면 규칙마다의 문장이 이미 말한다)."""
    if not decisions or not decisions.decisions:
        return []
    by_id = {v.id: v for v in violations}
    fixes = sum(1 for d in decisions.decisions if d.action == "fix")
    not_one = sum(1 for d in decisions.decisions if _is_dismissed(d))
    left = Counter(_left_reason(d, by_id.get(d.violation_id))[0] for d in decisions.decisions
                   if d.action == "skip" and not _is_dismissed(d))  # fmt: skip
    line = f"수정 판단: 고칠 대상 {fixes}건, {LEFT} {sum(left.values())}건"
    if len(left) > 1:
        line += f" ({', '.join(f'{label} {n}건' for label, n in left.items())})"
    if not_one:
        line += f", 모델이 위반이 아니라고 판단함 {not_one}건 (아래 접힌 블록)"
    line += f" · 모델 호출 {decisions.calls}회 · 저장된 판단 {decisions.cached}건 사용"
    if decisions.calls:
        line += f" · 토큰 입력 {decisions.input_tokens}, 출력 {decisions.output_tokens}"
    lines = ["", line]
    capped = sum(1 for d in decisions.decisions if d.reason == "call_limit")
    if capped:
        lines += ["", f"모델 호출 상한에 걸려 처리하지 못함: 수정 판단 {capped}건. {RERUN_HINT}"]
    return lines


def _is_dismissed(d: FixDecision) -> bool:
    return d.reason == "not_a_violation" and d.decided_by == "model"


def _dismissed_lines(set_aside: Sequence[tuple[Violation, str]]) -> list[str]:
    """도구가 잡았지만 모델이 위반이 아니라고 판단한 것 (접는 블록 안). 사람이 뒤집어 볼 수 있게
    위치와 모델의 까닭을 남긴다."""
    if not set_aside:
        return []
    lines = ["", f"도구가 잡았지만 모델이 위반이 아니라고 판단함 {len(set_aside)}건:"]
    for v, why in set_aside[:MAX_FOLDED_VIOLATIONS]:
        lines.append(f"- `{v.rule_id}` {_where(v)} · {inline_code(why, MAX_DETAIL_CHARS)}")
    if len(set_aside) > MAX_FOLDED_VIOLATIONS:
        lines.append(f"- 외 {len(set_aside) - MAX_FOLDED_VIOLATIONS}건")
    return lines


def _test_lines(found: Found, view: FixView) -> list[str]:
    """그 묶음이 고친 위반을 어느 테스트가 실행했는지와 만든 테스트 (4.9). 상태와 파일마다 줄 번호를
    모아 한 줄로 쓴다. 고친 것이 없으면 빈 목록."""
    match view.fix:
        case ModelFix() as fix if fix.ready:
            ids, runs, tests = fix.targets, fix.line_runs, fix.generated
        case MechanicalFix() as fix if fix.ready:
            ids, runs, tests = fix.fixed, (), ()
        case _:
            return []
    wanted = set(ids)
    violations = [v for v in found.violations if v.id in wanted]
    if not violations:
        return []
    runs = runs or runs_without_new_tests(violations)
    return ["테스트:", *status_lines(runs, violations), *generated_lines(tests)]


def _fix_lines(view: FixView, bundle: str, tests: Sequence[str] = ()) -> list[str]:
    """묶음 하나의 결과: 수정안 PR 번호, 만들지 못한 이유, 크기 상한 건수, 호출 수 (4.9, 9.4).
    tests는 고친 위반의 테스트 표시(`_test_lines`)다."""
    label = _LABEL[bundle]
    ref = f" #{view.number}" if view.number is not None else ""
    match view.state:
        case "running":
            line = f"fix로 정한 위반 {view.targets}건의 수정안을 만드는 중입니다."
        case "approval":
            line = (
                "base의 `.pipeline.yaml`이 `require_approval: true`라 수정안을 아직 만들지 "
                f"않았습니다. `/fix {bundle}`을 달면 만듭니다."
            )
        case "merged":
            line = f"이 커밋은 {label} PR{ref}을 머지한 결과라 같은 수정안을 다시 만들지 않습니다."
        case "open":
            line = f"이 커밋의 {label} PR{ref}이 열려 있습니다. 그 PR을 확인해 주세요."
        case "rejected":
            line = f"이 커밋의 {label} PR{ref}이 머지되지 않고 닫혀 다시 만들지 않습니다."
        case "done":
            if isinstance(view.fix, MechanicalFix):
                return _mechanical_result(view, view.fix, tests)
            return _fix_result(view, tests)
    lines = ["", f"**{label}:** {line}"]
    if view.size_capped:
        lines.append(f"수정안 크기 상한으로 처리하지 못함: {view.size_capped}건")
    return lines


def _mechanical_result(view: FixView, fix: MechanicalFix, tests: Sequence[str] = ()) -> list[str]:
    """기계적 수정 묶음의 결과. 모델을 부르지 않았으므로 호출 수가 없다."""
    proposal = view.proposal
    if not fix.ready:
        line = _FIX_FAILED[fix.status]
        if fix.detail:
            line += f" ({inline_code(fix.detail, MAX_DETAIL_CHARS)})"
        if fix.format_skipped:
            line += f". `ruff format`은 적용하지 않음 ({inline_code(fix.format_skipped, 200)})"
        return ["", f"**{_LABEL[MECHANICAL_BUNDLE]}:** {line}"]
    what = f"위반 {len(fix.fixed)}건을 고쳤습니다" if fix.fixed else "포맷만 고쳤습니다"
    line = (
        f"{what} (도구의 안전한 수정). 검증: 고친 뒤 테스트 {fix.test_line} (수정 전과 같은 결과), "
        "새 위반 없음."
    )
    if proposal is not None and proposal.number is not None:
        verb = "갱신했습니다" if proposal.updated else "열었습니다"
        line += f" 수정안 PR #{proposal.number}을 {verb}."
    elif proposal is not None:
        line += f" 수정안 PR을 만들지 않았습니다 ({proposal.reason})."
    lines = ["", f"**{_LABEL[MECHANICAL_BUNDLE]}:** {line}", *tests]
    if fix.formatted_files:
        lines.append(f"`ruff format`을 고친 파일 {len(fix.formatted_files)}개에 적용했습니다.")
    elif fix.format_skipped:
        lines.append(
            f"`ruff format`은 적용하지 않았습니다 ({inline_code(fix.format_skipped, 200)})."
        )
    if fix.remaining:
        lines.append(f"자동으로 고치지 못한 위반 {len(fix.remaining)}건은 그대로 남습니다.")
    return lines + _patch_block(view, proposal)


def _patch_block(view: FixView, proposal: ProposalResult | None) -> list[str]:
    """수정안 PR을 열지 못했을 때 검증을 통과한 패치를 접어 보인다. 비밀값이면 싣지 않는다."""
    shown = proposal is not None and proposal.number is None
    if not shown or proposal.secret_blocked or not view.patch:
        return []
    patch = view.patch
    if len(patch) > MAX_PATCH_CHARS:
        patch = patch[:MAX_PATCH_CHARS] + "\n… (생략)"
    fence = code_fence(patch)
    return ["", "<details><summary>수정 내용</summary>", "", f"{fence}diff", patch, fence, "",
            "</details>"]  # fmt: skip


def _fix_result(view: FixView, tests: Sequence[str] = ()) -> list[str]:
    fix, proposal = view.fix, view.proposal
    if not isinstance(fix, ModelFix):
        return []
    if fix.status == "budget":
        line = (
            f"모델 호출 상한에 걸려 처리하지 못함: 수정 {len(fix.targets)}건 "
            f"({inline_code(fix.detail, MAX_DETAIL_CHARS)}). {RERUN_HINT}"
        )
    elif not fix.ready:
        line = f"수정안을 만들지 못함: {_FIX_FAILED[fix.status]}"
        if fix.detail:
            line += f" ({inline_code(fix.detail, MAX_DETAIL_CHARS)})"
    else:
        check = fix.verification
        line = f"위반 {len(fix.targets)}건을 고쳤습니다."
        if check is not None:
            line += (
                f" 검증: 테스트 {check.tests}개가 수정 전과 같은 결과로 통과, 대상 위반 "
                f"{check.resolved}건 해소, 새 위반 없음."
            )
        if proposal is not None and proposal.number is not None:
            verb = "갱신했습니다" if proposal.updated else "열었습니다"
            line += f" 수정안 PR #{proposal.number}을 {verb}."
        elif proposal is not None:
            line += f" 수정안 PR을 만들지 않았습니다 ({proposal.reason})."
    lines = ["", f"**{_LABEL['model']}:** {line}", *tests]
    if view.size_capped:
        lines.append(f"수정안 크기 상한으로 처리하지 못함: {view.size_capped}건")
    lines.append(_fix_usage(fix))
    return lines + (_patch_block(view, proposal) if fix.ready else [])


def _fix_usage(fix: ModelFix) -> str:
    """수정 작성의 모델 호출 (9.4). 상한은 PR 상한이다."""
    line = f"수정 작성: 모델 호출 {fix.calls}회, 회차 {fix.attempts}"
    if fix.models:
        line += f" · {', '.join(inline_code(m, 80) for m in dict.fromkeys(fix.models))}"
    line += f" · 저장된 결과 {fix.cache_hits}건 사용"
    if fix.calls:
        line += f" · 토큰 입력 {fix.input_tokens}, 출력 {fix.output_tokens}"
    usage = fix.tool_usage
    if usage.calls or usage.limited:
        line += (
            f" · 도구 호출 {usage.calls}회 (규칙 검사 {usage.check_rules}, 테스트 "
            f"{usage.run_tests}), {usage.seconds:.0f}초"
        )
        if usage.limited:
            line += " (상한에 닿음)"
    return line


def _left_reason(d: FixDecision, v: Violation | None) -> tuple[str, str]:
    """고치지 않은 이유의 (짧은 이름, 문장). 알리기만 하는 규칙은 위반의 모양(파일 단위), 도구의
    자동 수정이 없음, 규칙의 수정 방법 `none`으로 나눈다. 모르는 이유(이전 체크포인트 등)는
    파이프라인이 쓴 이유를 쓴다."""
    reason = d.reason or "undecided"
    if reason == "notify_only":
        if v is not None and v.unit == "file":
            return _LEFT_REASON["notify_file"]
        return _LEFT_REASON["notify_tool" if d.why == NO_AUTOFIX_WHY else "notify_rule"]
    if reason == "no_safety_net":
        return _LEFT_REASON["flaky" if "불안정한 테스트" in d.why else "no_tests"]
    if reason == "undecided" and d.why in _UNDECIDED:
        return _UNDECIDED[d.why]
    if reason == "tests_failed":
        head = _TESTS_BROKEN_HEAD.get(d.why, "테스트가 통과하지 않아")
        sentence = f"{head} 고친 것을 확인할 수 없으므로 수정안을 만들지 않았습니다."
        return d.why or "테스트가 실패함", sentence
    if reason in _SKIP_REASONS and reason in _LEFT_REASON:
        return _LEFT_REASON[reason]
    why = d.why or _LEFT_REASON["undecided"][0]
    return why, f"수정안을 만들지 않았습니다 ({why})."


def _fix_note(d: FixDecision, v: Violation, number: int | None) -> str:
    """고칠 대상 줄 끝: 수정 방법, 수정안 PR 번호 (4.9). 테스트 표시는 수정 결과 아래에 상태와
    파일마다 묶는다. 모델이 쓴 이유는 코드 스팬 안에만 넣는다."""
    how = "기계적 수정" if d.method == "mechanical" else "모델 수정"
    if d.decided_by == "model" and d.why:
        how += f": {inline_code(d.why, MAX_DETAIL_CHARS)}"
    pr = f" · 수정안 #{number}" if number is not None else ""
    return f" · 고칠 대상 ({how}){pr}"


def _left_note(d: FixDecision, v: Violation, *, mixed: bool) -> str:
    """직접 고쳐야 하는 위치 끝에 붙는 글. 까닭의 문장은 규칙마다 한 번 적으므로 줄마다 되풀이하지
    않는다. 위치마다 다른 것만 붙인다: 모델이 쓴 이유 글(코드 스팬 안), 거절된 수정안의 번호.
    mixed면(한 규칙에 이유가 여럿) 어느 이유인지 짧은 이름을 붙인다."""
    if d.reason == "rejected_before" and d.why:
        return f" · {d.why}"
    short = _left_reason(d, v)[0] if mixed else ""
    model = inline_code(d.why, MAX_DETAIL_CHARS) if d.decided_by == "model" and d.why else ""
    if short and model:
        return f" · {short}: {model}"
    return f" · {short or model}" if short or model else ""


def _problem(rule: Rule, violations: Sequence[Violation]) -> list[str]:
    """규칙 블록의 "무엇이 문제인가" 한 줄. 그 규칙의 위반들이 같은 메시지(검사기·도구의
    메시지, 모델 판단의 까닭)를 가지면 규칙 블록에 한 번 싣는다. 메시지가 위치마다 보이는
    규칙(도구 설정의 규칙), 메시지가 없거나 서로 다른 것, 규칙 원문과 사실상 같은 말인 것은
    싣지 않는다."""
    messages = {v.message.strip() for v in violations}
    if len(messages) != 1 or any(v.rule_id == v.code for v in violations):
        return []
    message = messages.pop()
    if not message or _plain(message) == _plain(rule.text):
        return []
    return [f"  {PROBLEM} {inline_code(message, MAX_DETAIL_CHARS)}"]


def _plain(text: str) -> str:
    """공백과 마침표를 빼고 대소문자를 맞춘 글. 같은 말인지 견줄 때만 쓴다."""
    return "".join(c for c in text if not c.isspace() and c != ".").casefold()


def _left_lines(left: Sequence[tuple[Violation, FixDecision]]) -> tuple[list[str], bool]:
    """규칙 하나의 "직접 고쳐야 함 N건" 줄과 까닭의 문장, 이유가 여럿인지. 이유가 하나면 같은
    줄에, 여럿이면 이유마다 건수와 함께 한 줄씩."""
    reasons = Counter(_left_reason(d, v) for v, d in left)
    if len(reasons) == 1:
        ((_, sentence),) = reasons
        return [f"  {LEFT} {len(left)}건 · {sentence}"], False
    lines = [f"  {LEFT} {len(left)}건"]
    lines += [f"  - {n}건: {sentence}" for (_, sentence), n in reasons.items()]
    return lines, True


@dataclass
class _Fold:
    """접은 목록에 더 실을 수 있는 위반 수 (코멘트 전체에서 하나)."""

    left: int = MAX_FOLDED_VIOLATIONS

    def lines(self, summary: str, items: Sequence[str]) -> list[str]:
        shown = list(items[: self.left])
        self.left -= len(shown)
        out = ["", f"<details><summary>{summary}</summary>", "", *shown]
        if len(items) > len(shown):
            out.append(f"  - 외 {len(items) - len(shown)}건")
        return [*out, "", "</details>", ""]


def _violations(
    found: Found, decisions: Mapping[str, FixDecision], fixed_in: Mapping[str, int]
) -> list[str]:
    """규칙별 건수, 무엇이 문제인지, 고칠 대상의 위치, 직접 고쳐야 하는 위반과 수정안을 만들지
    않은 까닭 (4.9, 11.2 D6).

    고칠 대상은 규칙마다 앞 몇 건이 보이고 나머지는 접는다. 직접 고쳐야 하는 위반은 건수와 까닭의
    문장(규칙마다 한 번) 아래에 위치를 접는다. 수정 판단 전(테스트 전에 끝남)이면 위치만 앞 몇 건
    보인다. fixed_in은
    {위반 id: 그 위반을 고친 수정안 PR 번호}다. 접은 목록은 코멘트 전체에서 상한이 있다.
    """
    if not found.violations:
        return []
    by_rule = outcomes_by_rule(found)
    grouped: dict[str, list[Violation]] = {}
    for v in found.violations:
        grouped.setdefault(v.rule_id, []).append(v)
    lines = ["", "위반:"]
    fold = _Fold()
    if any(len(vs) > MAX_SHOWN_PER_RULE for vs in grouped.values()):
        lines[1:1] = ["", MANY_VIOLATIONS_NOTE]
    for rule_id, violations in grouped.items():
        outcome = by_rule[rule_id]
        lines += [
            f"- `{rule_id}` {_quote(outcome.rule)} · {len(violations)}건",
            f"  출처: {_source(outcome.rule)} · {_how_checked(outcome)}",
            *_problem(outcome.rule, violations),
        ]
        shown: list[str] = []
        left: list[tuple[Violation, FixDecision]] = []
        for v in violations:
            d = decisions.get(v.id)
            if d is None:
                shown.append(f"  - {_where(v)}")
            elif d.action == "fix":
                shown.append(f"  - {_where(v)}{_fix_note(d, v, fixed_in.get(v.id))}")
            else:
                left.append((v, d))
        lines += shown[:MAX_SHOWN_PER_RULE]
        if len(shown) > MAX_SHOWN_PER_RULE:
            rest = shown[MAX_SHOWN_PER_RULE:]
            lines += fold.lines(f"<code>{rule_id}</code>의 나머지 {len(rest)}건", rest)
        if left:
            head, mixed = _left_lines(left)
            lines += head
            lines += fold.lines(f"{LEFT} {len(left)}건의 위치",
                                [f"  - {_where(v)}{_left_note(d, v, mixed=mixed)}"
                                 for v, d in left])  # fmt: skip
    return lines


def fixed_in(fixes: Mapping[str, FixView]) -> dict[str, int]:
    """{위반 id: 그 위반을 고친 수정안 PR 번호}. 이번 검사가 열었거나 갱신한 수정안만 안다."""
    out: dict[str, int] = {}
    for view in fixes.values():
        number = view.proposal.number if view.proposal is not None else None
        if number is None:
            continue
        if isinstance(view.fix, ModelFix) and view.fix.ready:
            out |= dict.fromkeys(view.fix.targets, number)
        elif isinstance(view.fix, MechanicalFix) and view.fix.ready:
            out |= dict.fromkeys(view.fix.fixed, number)
    return out


def _where(v: Violation) -> str:
    if v.unit == "file":
        return f"{inline_code(v.file, 200)} (파일 이름·경로)"
    where = inline_code(f"{v.file}:{v.line}", 200)
    # 도구 설정 규칙은 원문이 "설정대로 통과한다" 하나라 위반마다 도구의 메시지를 보인다
    return f"{where} {inline_code(v.message, MAX_DETAIL_CHARS)}" if v.rule_id == v.code else where


def _rule_lines(outcomes: Sequence[RuleUnchecked], *, detail: bool) -> list[str]:
    lines = []
    for o in outcomes:
        line = f"- `{o.rule.id}` {_quote(o.rule)}"
        if detail and o.detail:
            line += f" · {inline_code(o.detail, MAX_DETAIL_CHARS)}"
        lines.append(line)
    return lines


def _unchecked(outcomes: Sequence[RuleOutcome], kinds: frozenset[str]) -> list[RuleUnchecked]:
    return [o for o in outcomes if isinstance(o, RuleUnchecked) and o.kind in kinds]


def _not_checkable(outcomes: Sequence[RuleOutcome]) -> list[str]:
    rules = _unchecked(outcomes, frozenset({"no_checker"}))
    if not rules:
        return []
    head = f"읽었지만 아직 검사하지 못하는 규칙 {len(rules)}개 (예시를 통과하는 검사기가 없음):"
    return ["", head, *_rule_lines(rules, detail=True)]


def _other_targets(outcomes: Sequence[RuleOutcome], *, pr_checked: bool = False) -> list[str]:
    """PR 단위 확인을 마치지 못한 PR 단위 규칙(이전 체크포인트 등). 확인했으면 PR 단위 부분에
    나온다."""
    pr = [] if pr_checked else _unchecked(outcomes, frozenset({"pr"}))
    if not pr:
        return []
    head = (
        f"PR 단위 확인을 마치지 못한 규칙 {len(pr)}개 (커밋 메시지, PR 본문, 브랜치, 바뀐 파일 "
        "종류. 다음 검사나 `/recheck`가 확인합니다):"
    )
    return ["", head, *_rule_lines(pr, detail=True)]


def _run_broken(answers: Mapping[str, RunAnswer]) -> list[str]:
    """테스트·정적 검사의 결과를 보고 모델이 어겼다고 판단한 run 규칙 (4.5). 줄이 없어 수정안으로
    이어지지 않는다(12.2)."""
    broken = [a for a in answers.values() if a.verdict == "broken"]
    if not broken:
        return []
    lines = ["", "테스트·정적 검사 결과로 어긴 규칙 (줄이 아니라 PR에 걸려 수정안은 만들지 않음):"]
    for a in broken:
        lines += [f"- `{a.rule.id}` {_quote(a.rule)} · {inline_code(a.why, MAX_DETAIL_CHARS)}",
                  f"  근거: {a.basis}"]  # fmt: skip
    return lines


def _unresolved(found: Found, answers: Mapping[str, RunAnswer], flagged: set[str]) -> list[str]:
    """확인하지 못한 규칙과 까닭 (4.9). 모델이 보고 판단하지 못한 것, run 규칙을 묻지 못한 것,
    체크포인트에 남은 옛 결과. 없으면 이 부분이 없다."""
    lines = []
    for o in found.outcomes:
        head = f"- `{o.rule.id}` {_quote(o.rule)} · "
        if isinstance(o, RuleJudged) and _undecided(o, flagged):
            part = f" (바뀐 곳 {o.pairs}군데 중 {o.undecided}군데)" if o.undecided < o.pairs else ""
            why = inline_code(o.undecided_why, MAX_DETAIL_CHARS)
            lines.append(f"{head}모델이 코드를 보고 판단하지 못함{part}: {why}")
        elif o.rule.id in answers and answers[o.rule.id].verdict == "unknown":
            answer = answers[o.rule.id]
            lines.append(f"{head}모델이 테스트·정적 검사 결과로 판단하지 못함: "
                         f"{inline_code(answer.why, MAX_DETAIL_CHARS)}")  # fmt: skip
        elif isinstance(o, RuleUnchecked) and o.kind not in (*_UNCHECKED_TEXT, "run"):
            lines.append(head + OLD_KIND_TEXT)
    if not lines:
        return []
    return ["", f"확인하지 못한 규칙 {len(lines)}개:", *lines]


def _failed(outcomes: Sequence[RuleOutcome]) -> list[str]:
    rules = _unchecked(outcomes, _FAILED_KINDS)
    if not rules:
        return []
    counts = Counter(_UNCHECKED_TEXT[o.kind] for o in rules)
    why = ", ".join(f"{text} {n}개" for text, n in counts.items())
    return ["", f"이번 검사에서 검사하지 못한 규칙 {len(rules)}개 ({why}):",
            *_rule_lines(rules, detail=True)]  # fmt: skip


def _capped(outcomes: Sequence[RuleOutcome]) -> list[str]:
    rules = _unchecked(outcomes, frozenset({"cap"}))
    if not rules:
        return []
    return [
        "",
        f"모델 호출 상한에 걸려 처리하지 못함: 검사기 작성 {len(rules)}개 규칙 "
        f"({inline_code(rules[0].detail, MAX_DETAIL_CHARS)}). "
        "다음 검사(새 커밋이나 `/recheck`)가 남은 규칙부터 이어서 합니다.",
    ]


def _checker_details(found: Found, checkers: Mapping[str, Checker]) -> list[str]:
    """이번 검사에서 위반을 찾은 검사기의 본문과 모델이 쓴 예시 (접힘)."""
    found_by = {v.rule_id for v in found.violations}
    shown = [
        o for o in found.outcomes
        if isinstance(o, RuleChecked) and o.rule.id in found_by and o.checker.key in checkers
        and o.checker.kind not in TOOL_KINDS
    ]  # fmt: skip
    lines: list[str] = []
    for o in shown[:MAX_CHECKER_DETAILS]:
        checker = checkers[o.checker.key]
        lines += _checker_block(o.rule, checker)
    return lines


def _checker_block(rule: Rule, checker: Checker) -> list[str]:
    body = checker.body.rstrip("\n")
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "\n… (생략)"
    by_path = checker.kind == "path-pattern"
    language = {"ast-grep": "yaml", "python-ast": "python", "path-pattern": "json"}[checker.kind]
    fence = code_fence(body)
    lines = [
        "",
        f"<details><summary>모델이 쓴 검사기 <code>{checker.id[:8]}</code> ({checker.kind}), "
        f"규칙 <code>{rule.id}</code></summary>",
        "",
    ]
    if checker.model:
        lines += [f"작성한 모델: {inline_code(checker.model, 80)}", ""]
    lines += [
        f"{fence}{language}",
        body,
        fence,
    ]
    by_model = [e for e in checker.examples if e.written_by == "model"]
    from_doc = len(checker.examples) - len(by_model)
    lines += ["", f"예시: 문서 {from_doc}개, 모델이 쓴 것 {len(by_model)}개"]
    for e in by_model:
        if by_path:
            kind = "어기는 경로" if e.expect_lines else "지키는 경로"
        else:
            kind = f"위반 (줄 {', '.join(map(str, e.expect_lines))})" if e.expect_lines else "정상"
        example_fence = code_fence(e.code)
        lines += ["", f"모델이 쓴 예시, {kind}:", "",
                  f"{example_fence}{'text' if by_path else 'python'}", e.code.rstrip("\n"),
                  example_fence]  # fmt: skip
    return [*lines, "", "</details>"]


def sections_read(sections: Sequence[str]) -> list[str]:
    """규칙 문서가 커서 관련 절만 읽었을 때 (6.3). 읽지 않은 절의 규칙은 이 PR에 적용되지 않았다."""
    if not sections:
        return []
    shown = [f"- {inline_code(s, 200)}" for s in sections[:MAX_LISTED_SECTIONS]]
    if len(sections) > MAX_LISTED_SECTIONS:
        shown.append(f"- 외 {len(sections) - MAX_LISTED_SECTIONS}개")
    return [
        "", f"규칙 문서가 커서 관련 절 {len(sections)}개만 읽음. {SECTIONS_NOTE}",
        "", f"<details><summary>읽은 절 {len(sections)}개</summary>", "", *shown, "", "</details>",
    ]  # fmt: skip


def _retrievals(
    found: Found, decisions: Decided | None, fixes: Mapping[str, FixView]
) -> tuple[Retrieval, ...]:
    """이번 검사에서 프롬프트에 넣은 과거 기록 (검사기 작성, 수정 판단, 수정 작성 순서)."""
    items = list(found.retrievals)
    if decisions is not None and decisions.retrieval is not None:
        items.append(decisions.retrieval)
    for view in fixes.values():
        if isinstance(view.fix, ModelFix):
            items += view.fix.retrievals
    return merge_retrievals(items)


def references_block(retrievals: Sequence[Retrieval], rag: RagSwitch | None = None) -> list[str]:
    """검색 상태 한 줄(rag, 6.7)과 참고한 과거 기록 (6.6 끝, 4.9). 건마다 종류, 원래 PR과 수정안
    PR의 번호, 규칙 id, 유사도, 쓰인 단계. diff는 넣지 않는다(그 PR에 있다). 기준값에 못 미쳐
    버린 후보는 개수만 적는다. 검색을 켜지 않았으면 retrievals가 비어 상태 줄만 그리고, 그 줄도
    알릴 만한 때만 있다(`rag_switch_pure.comment_line`)."""
    line = comment_line(rag) if rag is not None else None
    status = ["", line] if line is not None else []
    rows = [(r, ref) for r in retrievals for ref in r.references]
    below = [(r.site, r.below) for r in retrievals if r.below]
    errors = sorted({r.error for r in retrievals if r.error})
    if not rows and not below and not errors:
        return status
    distinct = len({ref.record_id for _, ref in rows})
    lines = [*status, "", f"<details><summary>참고한 과거 기록 {distinct}건</summary>", ""]
    for r, ref in rows[:MAX_REFERENCES]:
        line = f"- 기록 {ref.record_id} · {KIND_LABEL.get(ref.kind, ref.kind)}"
        if ref.pr_number is not None:
            line += f" · 원래 PR #{ref.pr_number}"
        if ref.fix_pr_number is not None:
            line += f" · 수정안 PR #{ref.fix_pr_number}"
        line += f" · 규칙 {inline_code(ref.rule_id, 80)} · 유사도 {ref.similarity:.2f}"
        lines.append(line + f" · {SITE_LABEL.get(r.site, r.site)}")
    if len(rows) > MAX_REFERENCES:
        lines.append(f"- 외 {len(rows) - MAX_REFERENCES}건")
    if below:
        shown = ", ".join(f"{SITE_LABEL.get(site, site)} {n}건" for site, n in below)
        lines += ["", f"유사도 기준값에 못 미쳐 넣지 않은 후보: {shown}"]
    for error in errors:
        lines += ["", f"검색을 하지 못함: {inline_code(error, MAX_DETAIL_CHARS)}"]
    return [*lines, "", "</details>"]


def _read_rules(found: Found, answers: Mapping[str, RunAnswer] | None = None) -> list[str]:
    """읽은 규칙마다 출처, 범위, 확인 방법 (접는 블록 안)."""
    if not found.outcomes:
        return []
    lines = ["", f"읽은 규칙 {len(found.outcomes)}개:"]
    for o in found.outcomes[:MAX_LISTED_RULES]:
        rule = o.rule
        scope = ", ".join(inline_code(p, 100) for p in rule.scope) if rule.scope else "저장소 전체"
        lines += [
            f"- `{rule.id}` {_quote(rule)}",
            f"  출처: {_source(rule)} · 범위: {scope} · 확인: {_how_checked(o, answers)}",
        ]
        if rule.exclude:
            globs = ", ".join(inline_code(p, 100) for p in rule.exclude)
            line = f"  범위 추정: {globs} 제외 (문서에 범위 없음)"
            if rule.exclude_reason:
                line += f" · {inline_code(rule.exclude_reason, MAX_DETAIL_CHARS)}"
            lines.append(line)
        if rule.exclude_dropped:
            lines.append(
                "  범위 추정을 버림 (너무 많이 뺌. 규칙은 넓은 범위로 적용): "
                + inline_code(rule.exclude_dropped, MAX_DETAIL_CHARS)
            )
    hidden = len(found.outcomes) - MAX_LISTED_RULES
    if hidden > 0:
        lines.append(f"- 외 {hidden}개")
    return lines


def _size(lines: Sequence[str]) -> int:
    return sum(len(line) + 1 for line in lines)


def _fit(blocks: Sequence[list[str]], budget: int) -> list[str]:
    """앞의 블록부터 budget 안에 드는 것만 넣는다. 넘치면 생략 표시를 남기고 멈춘다."""
    out: list[str] = []
    for block in blocks:
        if _size(out) + _size(block) > budget:
            return [*out, "", OMITTED]
        out += block
    return out
