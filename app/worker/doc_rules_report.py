"""결과 코멘트의 규칙 줄과 규칙 절 (docs/DESIGN.md 4.9, 9.4). 판정을 새로 내지 않는다.

`DocRules` 값 하나와, 위반을 찾은 검사기의 본문(`checkers` 테이블에서 읽은 것), 위반마다의 수정
판단(`Decided`), 묶음마다의 수정 상태(`FixView`)만 받는다. 규칙의 인용, 문서의 절 이름, 파일 경로,
도구의 메시지, 검사기 본문과 예시, 실패 이유, 모델이 쓴 수정 판단의 이유는 모두 믿지 않는 글이라
코드 스팬이나 코드 울타리 안에만 넣는다 (app/worker/markdown.py).
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from app.worker import pr_check_report
from app.worker.checker_authoring_pure import JudgeRoute
from app.worker.checker_pure import TOOL_KINDS, Checker
from app.worker.doc_rules_pure import (
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
from app.worker.fix.fix_decision_pure import Decided, FixDecision
from app.worker.fix.fix_pure import MECHANICAL_BUNDLE, MechanicalFix
from app.worker.fix.model_fix_pure import ModelFix
from app.worker.fix.proposal_pure import ProposalResult
from app.worker.markdown import code_fence, inline_code
from app.worker.rag_pure import KIND_LABEL, SITE_LABEL, Retrieval, merge_retrievals
from app.worker.rag_switch_pure import RagSwitch, describe
from app.worker.rule_pure import Rule
from app.worker.rules.pr_check_pure import PrChecks
from app.worker.test_lines import generated_lines, runs_without_new_tests, status_lines
from app.worker.violation_pure import Violation

TITLE: Final = "**규칙:**"
VIOLATION: Final = "⚠️"
CLEAN: Final = "✅"
NO_RULES: Final = "규칙을 찾지 못해 규칙 검사를 하지 않음"
HOW_TO_WRITE: Final = (
    "규칙 문서(기본은 `README.md`, `CONTRIBUTING.md`)에 `## 코드 규칙` 같은 절을 두고 규칙을 "
    "한 문장씩 적거나 ruff 설정(`ruff.toml`, `pyproject.toml`의 `[tool.ruff]`)을 두면 다음 "
    "검사부터 그 규칙으로 검사합니다. "
    '예: "`orders/` 아래 모듈은 `print`를 쓰지 않는다." '
    "다른 문서에 적었으면 base 브랜치 `.pipeline.yaml`의 `rules.docs`에 그 경로를 적으세요."
)
HOW_TO_CORRECT: Final = (
    "규칙 해석이 틀렸으면 README의 그 문장을 고치거나 "
    "`.pipeline.yaml`의 `rules.disabled`에 규칙 id를 적으세요."
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

_UNCHECKED_TEXT: Final[Mapping[str, str]] = {
    "pr": "PR 단위 규칙 (PR 단위 확인으로 봄)",
    "human": "사람이 확인하는 규칙 (검사하지 않음)",
    "no_checker": "읽었지만 아직 검사하지 못함",
    "cap": "호출 상한에 걸려 이번에 처리하지 못함",
    "model_failed": "검사하지 못함 (모델 호출 실패)",
    "tool_unavailable": "검사하지 못함 (도구 준비 실패)",
    "run_failed": "검사하지 못함 (검사기 실행 실패)",
}
_FAILED_KINDS: Final = frozenset({"model_failed", "tool_unavailable", "run_failed"})
# 모델 판단으로 보낸 까닭 (4.4 "의미 규칙을 모델 판단으로 보내는 때"). none·part 뒤에는 모델이 적은
# "검사기로 가릴 수 없는 부분"을 붙인다
_JUDGE_ROUTE_TEXT: Final[Mapping[JudgeRoute, str]] = {
    "none": "검사기로 가릴 수 없는 규칙이라고 검사기 작성 모델이 답함",
    "part": "검사기로는 일부만 가릴 수 있다고 검사기 작성 모델이 답함",
    "examples": "검사기가 예시 확인을 두 번 통과하지 못함",
    "attempts": "예시를 통과하는 검사기를 만들지 못함",
}
# 파이프라인이 고치지 않은 위반의 이유 (4.9). 사람이 직접 고쳐야 하는 까닭으로 읽힌다
_LEFT_LABEL: Final[Mapping[str, str]] = {
    "rejected_before": "이전에 거절됨",
    "notify_only": "알리기만 하는 규칙",
    "tests_failed": "테스트가 실패함",
    "no_safety_net": "테스트로 확인할 수 없음",
    "team_pattern": "팀이 닫은 수정과 비슷함",
    "needs_other_file": "다른 파일이나 공개 이름을 바꿔야 함",
    "changes_behavior": "고치면 동작이 바뀜",
    "not_a_violation": "모델이 위반이 아니라고 봄",
    "risky": "모델이 고치지 않기로 함",
    "call_limit": "호출 상한",
    "undecided": "수정 판단을 하지 못함",
}
LEFT: Final = "직접 고쳐야 함"
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


# 확인 대상이 코드 줄이 아닌 규칙 (4.3). 검사하지 못한 것이 아니라 검사 대상이 아니다
_OTHER_TARGETS: Final = frozenset({"pr", "human"})


def title(
    result: DocRules | None,
    fixes: Mapping[str, FixView] | None = None,
    pr: PrChecks | None = None,
) -> str | None:
    """코멘트 맨 위의 "규칙:" 줄 (4.9). 규칙 경로를 돌기 전에 끝났으면 None. pr은 PR 단위 확인의
    결과다(PR 규칙이 있는 검사만). 그 위반은 코드 위반과 따로 센다."""
    match result:
        case None:
            return None
        case NotRun(reason=reason):
            return f"{TITLE} 검사하지 못함 ({inline_code(reason, MAX_DETAIL_CHARS)})"
        case NoRuleDocs() | NoRulesFound():
            return f"{TITLE} {NO_RULES}"
        case Found():
            numbers = [n for v in (fixes or {}).values() if (n := _proposal_number(v)) is not None]
            line = f"{TITLE} {_title_line(result, pr)}"
            return line + (f" · 수정안 {', '.join(f'#{n}' for n in numbers)}" if numbers else "")


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
) -> list[str]:
    """규칙 절의 줄들 ("규칙:" 줄 아래). checkers는 위반을 찾은 검사기의 {CheckerRef.key: 검사기}다.

    decisions는 테스트 뒤의 수정 판단이다. 아직 없으면(테스트 전에 끝남) 판단 없이 그린다.
    fixes는 묶음(mechanical, model)마다의 수정 상태다. 그 묶음으로 정한 위반이 없으면 없다.
    rag는 이 검사의 검색 상태다. 규칙을 찾은 검사에서 참고 기록 블록 앞에 한 줄로 보인다.
    """
    match result:
        case None | NotRun():
            return []
        case NoRuleDocs(unread=unread):
            lines = ["", HOW_TO_WRITE]
            if unread:
                lines += ["", "읽지 못한 규칙 문서:"]
                lines += [f"- {inline_code(d.path)}: {inline_code(d.reason)}" for d in unread]
            return lines
        case NoRulesFound():
            return ["", HOW_TO_WRITE]
        case Found():
            return _found(result, checkers, decisions, fixes or {}, rag, pr)


def _found(
    found: Found,
    checkers: Mapping[str, Checker],
    decisions: Decided | None,
    fixes: Mapping[str, FixView],
    rag: RagSwitch | None = None,
    pr: PrChecks | None = None,
) -> list[str]:
    head = (
        ["", f"규칙 문서를 읽지 못해 도구 설정의 규칙만 검사함 "
         f"({inline_code(found.note, MAX_DETAIL_CHARS)})"]
        if found.note else []
    )  # fmt: skip
    head += sections_read(found.sections)
    tail = ["", usage_line(found.usage), "", HOW_TO_CORRECT]
    by_id = {d.violation_id: d for d in decisions.decisions} if decisions else {}
    blocks = [
        _judge_lines(found),
        _decision_summary(decisions),
        *(_fix_lines(view, bundle, _test_lines(found, view)) for bundle, view in fixes.items()),
        _violations(found, by_id, fixed_in(fixes)),
        pr_check_report.section(pr) if pr is not None else [],
        _not_checkable(found.outcomes),
        _failed(found.outcomes),
        _capped(found.outcomes),
        _other_targets(found.outcomes, pr_checked=pr is not None),
        _checker_details(found, checkers),
        references_block(_retrievals(found, decisions, fixes), rag),
        _read_rules(found),
    ]
    budget = MAX_SECTION_CHARS - _size(head) - _size(tail)
    return head + _fit(blocks, budget) + tail


def _title_line(found: Found, pr_checks: PrChecks | None = None) -> str:
    checked = sum(1 for o in found.outcomes if isinstance(o, RuleChecked)
                  or isinstance(o, RuleJudged) and not o.left)  # fmt: skip
    pr = len(_unchecked(found.outcomes, frozenset({"pr"})))
    human = len(_unchecked(found.outcomes, frozenset({"human"})))
    unchecked = len(found.outcomes) - checked - pr - human
    if found.violations:
        rules = len({v.rule_id for v in found.violations})
        line = f"{VIOLATION} 위반 {len(found.violations)}건 (규칙 {rules}개)"
    elif checked:
        line = f"{CLEAN} 위반 없음 (규칙 {checked}개 검사)"
    else:
        line = f"검사한 규칙 없음 (읽은 규칙 {len(found.outcomes)}개)"
    if unchecked and (found.violations or checked):
        line += f" · 검사하지 못한 규칙 {unchecked}개"
    if pr_checks is not None:
        line += pr_check_report.title_part(pr_checks)
    elif pr:
        line += f" · PR 단위 규칙 {pr}개"
    if human:
        line += f" · 사람이 확인하는 규칙 {human}개"
    if found.disabled:
        line += f" · `rules.disabled`로 끈 규칙 {found.disabled}개"
    return line


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


def _how_checked(outcome: RuleOutcome) -> str:
    if isinstance(outcome, RuleChecked):
        ref = outcome.checker
        if outcome.rule.origin == "tool_config":
            return f"도구 `ruff` (`{outcome.rule.source.file}`의 설정)"
        if ref.kind in TOOL_KINDS:
            codes = f" {', '.join(f'`{c}`' for c in ref.codes)}" if ref.codes else ""
            return f"도구 `{ref.kind}`{codes} (모델이 도구 목록에서 고르고 예시로 확인함)"
        saved = ", 저장된 것" if ref.cached else ""
        return f"검사기 `{ref.id[:8]}` ({ref.kind}{saved})"
    if isinstance(outcome, RuleJudged):
        why = _JUDGE_ROUTE_TEXT[outcome.route]
        if outcome.route in ("none", "part") and outcome.reason:
            why += f": {inline_code(outcome.reason, MAX_DETAIL_CHARS)}"
        left = f", 이번에 판단하지 못한 hunk {outcome.left}개" if outcome.left else ""
        return f"모델 판단 ({why}{left})"
    return _UNCHECKED_TEXT[outcome.kind]


def _judge_lines(found: Found) -> list[str]:
    """모델 판단의 규칙 수와 호출 (9.4), 판단하지 못한 규칙 (4.9의 호출 상한 줄)."""
    usage = found.judge
    judged = [o for o in found.outcomes if isinstance(o, RuleJudged)]
    if usage is None or not judged:
        return []
    line = (
        f"모델 판단: 규칙 {usage.rules}개, hunk {usage.hunks}개 · 모델 호출 {usage.calls}회 · "
        f"저장된 판단 {usage.cached}건 사용"
    )
    if usage.models:
        line += f" · {', '.join(inline_code(m, 80) for m in usage.models)}"
    if usage.dropped:
        line += f" · 버린 답 {usage.dropped}건 (PR이 바꾼 줄을 그대로 인용하지 않음)"
    if usage.calls:
        line += f" · 토큰 입력 {usage.input_tokens}, 출력 {usage.output_tokens}"
    lines = ["", line]
    capped = [o for o in judged if o.left_kind == "cap"]
    if capped:
        why = inline_code(capped[0].left_detail, MAX_DETAIL_CHARS)
        lines += ["", f"모델 호출 상한에 걸려 처리하지 못함: 위반 판단 {len(capped)}개 규칙 "
                  f"(hunk {sum(o.left for o in capped)}개, {why}). {RERUN_HINT}"]  # fmt: skip
    failed = [o for o in judged if o.left_kind == "model_failed"]
    if failed:
        lines += ["", f"모델 판단을 하지 못한 규칙 {len(failed)}개 (모델 호출 실패. 다음 검사가 "
                  "다시 묻습니다):"]  # fmt: skip
        for o in failed:
            why = inline_code(o.left_detail, MAX_DETAIL_CHARS)
            lines.append(f"- `{o.rule.id}` {_quote(o.rule)} · {why}")
    return lines


def _decision_summary(decisions: Decided | None) -> list[str]:
    """고칠 대상과 직접 고쳐야 하는 위반의 건수, 이유별 건수, 수정 판단의 모델 호출 (4.9)."""
    if not decisions or not decisions.decisions:
        return []
    fixes = sum(1 for d in decisions.decisions if d.action == "fix")
    left = Counter(_left_label(d) for d in decisions.decisions if d.action == "skip")
    line = f"수정 판단: 고칠 대상 {fixes}건, {LEFT} {sum(left.values())}건"
    if left:
        line += f" ({', '.join(f'{label} {n}건' for label, n in left.items())})"
    line += f" · 모델 호출 {decisions.calls}회 · 저장된 판단 {decisions.cached}건 사용"
    if decisions.calls:
        line += f" · 토큰 입력 {decisions.input_tokens}, 출력 {decisions.output_tokens}"
    lines = ["", line]
    capped = sum(1 for d in decisions.decisions if d.reason == "call_limit")
    if capped:
        lines += ["", f"모델 호출 상한에 걸려 처리하지 못함: 수정 판단 {capped}건. {RERUN_HINT}"]
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


def _left_label(d: FixDecision) -> str:
    """고치지 않은 이유의 이름. 모르는 이유(이전 체크포인트 등)는 파이프라인이 쓴 이유를 쓴다."""
    return _LEFT_LABEL.get(d.reason or "undecided") or d.why or _LEFT_LABEL["undecided"]


def _fix_note(d: FixDecision, v: Violation, number: int | None) -> str:
    """고칠 대상 줄 끝: 수정 방법, 수정안 PR 번호 (4.9). 테스트 표시는 수정 결과 아래에 상태와
    파일마다 묶는다. 모델이 쓴 이유는 코드 스팬 안에만 넣는다."""
    how = "기계적 수정" if d.method == "mechanical" else "모델 수정"
    if d.decided_by == "model" and d.why:
        how += f": {inline_code(d.why, MAX_DETAIL_CHARS)}"
    pr = f" · 수정안 #{number}" if number is not None else ""
    return f" · 고칠 대상 ({how}){pr}"


def _left_note(d: FixDecision) -> str:
    """직접 고쳐야 하는 줄 끝의 이유. 모델의 이유 글은 코드 스팬 안에만 넣는다."""
    label = _left_label(d)
    if d.decided_by == "model" and d.why:
        return f" · {label}: {inline_code(d.why, MAX_DETAIL_CHARS)}"
    if d.why and d.why != label:
        return f" · {d.why}"
    return f" · {label}"


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
    """규칙별 건수, 고칠 대상의 위치, 직접 고쳐야 하는 위반의 이유별 건수 (4.9, 11.2 D6).

    고칠 대상은 규칙마다 앞 몇 건이 보이고 나머지는 접는다. 직접 고쳐야 하는 위반은 이유별 건수 한
    줄 아래에 위치를 접는다. 수정 판단 전(테스트 전에 끝남)이면 위치만 앞 몇 건 보인다. fixed_in은
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
            reasons = Counter(_left_label(d) for _, d in left)
            why = ", ".join(f"{label} {n}건" for label, n in reasons.items())
            lines.append(f"  {LEFT} {len(left)}건 ({why})")
            lines += fold.lines(f"{LEFT} {len(left)}건의 위치",
                                [f"  - {_where(v)}{_left_note(d)}" for v, d in left])  # fmt: skip
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
    """코드 줄로 확인하지 않는 규칙의 목록. 사람이 확인하는 규칙은 보이기만 한다. PR 단위 규칙은
    PR 단위 확인을 했으면 그 부분에 나오고, 하지 못했으면(이전 체크포인트 등) 목록으로만 보인다."""
    lines: list[str] = []
    pr = [] if pr_checked else _unchecked(outcomes, frozenset({"pr"}))
    if pr:
        head = (
            f"PR 단위로 확인할 규칙 {len(pr)}개 (커밋 메시지, PR 본문, 브랜치, 바뀐 파일 종류. "
            "아직 확인하지 않음):"
        )
        lines += ["", head, *_rule_lines(pr, detail=True)]
    human = _unchecked(outcomes, frozenset({"human"}))
    if human:
        head = f"사람이 확인하는 규칙 {len(human)}개 (파이프라인이 검사하지 않음):"
        lines += ["", head, *_rule_lines(human, detail=True)]
    return lines


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
    버린 후보는 개수만 적는다. 검색을 켜지 않았으면 retrievals가 비어 상태 줄만 그린다."""
    status = ["", describe(rag)] if rag is not None else []
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


def _read_rules(found: Found) -> list[str]:
    if not found.outcomes:
        return []
    lines = ["", f"<details><summary>읽은 규칙 {len(found.outcomes)}개</summary>", ""]
    for o in found.outcomes[:MAX_LISTED_RULES]:
        rule = o.rule
        scope = ", ".join(inline_code(p, 100) for p in rule.scope) if rule.scope else "저장소 전체"
        lines += [
            f"- `{rule.id}` {_quote(rule)}",
            f"  출처: {_source(rule)} · 범위: {scope} · 확인: {_how_checked(o)}",
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
    return [*lines, "", "</details>"]


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
