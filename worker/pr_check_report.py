"""결과 코멘트의 PR 단위 확인 부분 (docs/DESIGN.md 4.5 끝, 4.9). 판정을 새로 내지 않는다.

`PrChecks` 하나만 받는다. 규칙의 인용, 커밋 제목, PR 글의 인용, 모델의 이유, 패턴, 실패 이유는 모두
믿지 않는 글이라 코드 스팬 안에만 넣는다 (worker/markdown.py). 커밋은 짧은 해시로 보인다.
코드 위반과 따로 된 목록이고, 수정안을 만들지 않는다는 것을 머리줄에 적는다 (4-13).
"""

import json
from collections.abc import Mapping
from typing import Final

from worker.markdown import inline_code
from worker.rule_pure import Rule
from worker.rules.pr_check_pure import FACT_LABEL, WHERE_LABEL, PrChecks, PrRuleResult, PrViolation

VIOLATION: Final = "⚠️"
HEAD: Final = "PR 단위 확인 (알리기만 하고 수정안은 만들지 않음)"
MAX_SHOWN_PER_RULE: Final = 10
MAX_QUOTE_CHARS: Final = 200
MAX_RULE_CHARS: Final = 500
MAX_DETAIL_CHARS: Final = 300
RERUN_HINT: Final = "`/fix`로 다시 돌릴 수 있습니다."
# 제목·본문만 고친 것(pull_request.edited)은 아직 다시 확인하지 않는다 (DESIGN 4.5 끝, 12.2)
RECHECK_HINT: Final = (
    "PR 제목·본문을 고쳤으면 `/recheck`를 달아 다시 확인하세요. 커밋 없이 고친 것은 저절로 다시 "
    "보지 않습니다."
)
_UNCHECKED_TEXT: Final[Mapping[str, str]] = {
    "plan_none": "PR 사실로 확인할 수 없는 규칙이라고 모델이 답함",
    "cap": "호출 상한에 걸려 확인 방법을 정하지 못함 (다음 검사가 이어서 합니다)",
    "model_failed": "확인 방법을 정하는 모델 호출이 실패함",
    "tool_unavailable": "샌드박스 도구를 준비하지 못함",
    "run_failed": "패턴을 이 PR에 맞추지 못함",
    "no_facts": "PR 사실을 읽지 못함",
}


def title_part(checks: PrChecks) -> str:
    """ "규칙:" 줄 끝에 붙는 PR 단위 확인의 요약. 코드 위반의 건수와 따로 센다."""
    if checks.violations:
        rules = len({v.rule_id for v in checks.violations})
        return f" · {VIOLATION} PR 단위 위반 {len(checks.violations)}건 (규칙 {rules}개)"
    seen = sum(1 for r in checks.results if r.status in ("checked", "not_applicable"))
    part = f" · PR 단위 규칙 {seen}개 위반 없음" if seen else ""
    missed = len(checks.results) - seen
    if missed:
        part += f" · PR 단위 규칙 {missed}개 확인하지 못함"
    return part


def section(checks: PrChecks) -> list[str]:
    lines = ["", f"**{HEAD}:** {_summary(checks)}"]
    if checks.note:
        lines.append(
            f"PR 사실을 읽지 못해 확인하지 않음: {inline_code(checks.note, MAX_DETAIL_CHARS)}"
        )
    lines += _violations(checks)
    if any(v.where in ("pr_body", "pr_title") for v in checks.violations):
        lines.append(RECHECK_HINT)
    clean = [r for r in checks.results if r.status in ("checked", "not_applicable")
             and not any(v.rule_id == r.rule.id for v in checks.violations)]  # fmt: skip
    if clean:
        lines += ["", "PR 단위 규칙 중 위반 없음:"]
        lines += [f"- `{r.rule.id}` {_quote(r.rule)} · {_how(r)}" for r in clean]
    partial = [r for r in checks.results if r.partial]
    if partial:
        lines += ["", "상한을 넘어 일부만 확인한 PR 단위 규칙:"]
        lines += [f"- `{r.rule.id}` · {inline_code(r.partial, MAX_DETAIL_CHARS)}" for r in partial]
    capped = [r for r in checks.results if r.status == "left" and r.kind == "cap"]
    if capped:
        why = inline_code(capped[0].detail, MAX_DETAIL_CHARS)
        lines += ["", f"모델 호출 상한에 걸려 처리하지 못함: PR 단위 판단 {len(capped)}개 규칙 "
                  f"({why}). {RERUN_HINT}"]  # fmt: skip
    failed = [r for r in checks.results if r.status == "left" and r.kind != "cap"]
    unchecked = [r for r in checks.results if r.status == "unchecked"]
    if failed or unchecked:
        lines += ["", f"확인하지 못한 PR 단위 규칙 {len(failed) + len(unchecked)}개:"]
        for r in failed:
            why = inline_code(r.detail, MAX_DETAIL_CHARS)
            lines.append(f"- `{r.rule.id}` {_quote(r.rule)} · 모델 판단 실패 (다음 검사가 다시 "
                         f"묻습니다): {why}")  # fmt: skip
        for r in unchecked:
            text = _UNCHECKED_TEXT.get(r.kind or "", "확인하지 못함")
            detail = f": {inline_code(r.detail, MAX_DETAIL_CHARS)}" if r.detail else ""
            lines.append(f"- `{r.rule.id}` {_quote(r.rule)} · {text}{detail}")
    return lines


def _summary(checks: PrChecks) -> str:
    usage = checks.usage
    parts = [f"규칙 {len(checks.results)}개"]
    if not checks.note:
        skipped = []
        if checks.skipped_merge:
            skipped.append(f"merge 커밋 {checks.skipped_merge}개")
        if checks.skipped_pipeline:
            skipped.append(f"파이프라인 수정안 커밋 {checks.skipped_pipeline}개")
        commits = f"커밋 {checks.commits}개"
        if skipped:
            commits += f" (보지 않음: {', '.join(skipped)})"
        parts.append(commits)
    calls = f"모델 호출 {usage.plan_calls + usage.judge_calls}회"
    if usage.plan_calls or usage.judge_calls:
        calls += f" (확인 방법 정하기 {usage.plan_calls}, 판단 {usage.judge_calls})"
    parts.append(calls)
    parts.append(f"저장된 결과 {usage.cached}건 사용")
    if usage.models:
        parts.append(", ".join(inline_code(m, 80) for m in usage.models))
    if usage.dropped:
        parts.append(f"버린 답 {usage.dropped}건 (이 PR의 커밋이나 글을 그대로 가리키지 않음)")
    if usage.plan_calls or usage.judge_calls:
        parts.append(f"토큰 입력 {usage.input_tokens}, 출력 {usage.output_tokens}")
    return " · ".join(parts)


def _violations(checks: PrChecks) -> list[str]:
    if not checks.violations:
        return []
    by_rule = {r.rule.id: r for r in checks.results}
    grouped: dict[str, list[PrViolation]] = {}
    for v in checks.violations:
        grouped.setdefault(v.rule_id, []).append(v)
    lines = ["", "PR 단위 위반:"]
    for rule_id, found in grouped.items():
        result = by_rule[rule_id]
        lines += [
            f"- `{rule_id}` {_quote(result.rule)} · {len(found)}건",
            f"  출처: {_source(result.rule)} · 확인: {_how(result)}",
        ]
        lines += [f"  - {_where(v)}" for v in found[:MAX_SHOWN_PER_RULE]]
        if len(found) > MAX_SHOWN_PER_RULE:
            lines.append(f"  - 외 {len(found) - MAX_SHOWN_PER_RULE}건")
    return lines


def _where(v: PrViolation) -> str:
    place = f"커밋 `{v.sha[:7]}`" if v.where == "commit" else WHERE_LABEL[v.where]
    if v.quote:
        place += f" {inline_code(v.quote, MAX_QUOTE_CHARS)}"
    elif v.where in ("pr_body", "pr_title"):
        place += " (빠진 내용)"
    if v.why:
        place += f" · {inline_code(v.why, MAX_DETAIL_CHARS)}"
    return place


def _how(r: PrRuleResult) -> str:
    decl = r.decl
    if decl is None:
        return "확인 방법 없음"
    gate = f", 바뀐 파일이 {_regex(decl.when_changed)}에 맞을 때만" if decl.when_changed else ""
    if r.method == "pattern":
        verb = "맞아야 함" if decl.condition == "must_match" else "맞으면 안 됨"
        how = f"PR 패턴 ({FACT_LABEL[decl.fact]}이 {_regex(decl.pattern)}에 {verb}{gate})"
    else:
        why = (
            "PR 패턴이 예시 확인을 두 번 통과하지 못함" if r.route == "examples"
            else "글의 뜻을 읽어야 한다고 모델이 답함"
        )  # fmt: skip
        how = f"모델 판단 ({FACT_LABEL[decl.fact]}, {why}{gate})"
    if r.status == "not_applicable":
        how += " · 이 PR은 바뀐 파일이 조건에 맞지 않아 해당 없음"
    return how


def _regex(pattern: str) -> str:
    """정규식은 JSON 문자열로 보인다. 앞뒤 공백과 줄바꿈이 뜻을 가진다 ("^feat: ")."""
    return inline_code(json.dumps(pattern, ensure_ascii=False), 160)


def _quote(rule: Rule) -> str:
    return inline_code(rule.text, MAX_RULE_CHARS)


def _source(rule: Rule) -> str:
    source = rule.source
    where = f"{source.file} > {source.section}" if source.section else source.file
    return inline_code(where, 200)
