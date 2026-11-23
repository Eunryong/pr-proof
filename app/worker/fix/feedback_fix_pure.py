"""받아들인 PR 코멘트를 수정 작성의 틀(4.7, 4.8)에 태우는 자료 (docs/DESIGN.md 4.11). 입출력이
없다.

받아들인 코멘트마다, 판단이 정한 고칠 파일마다 `Violation(found_by="feedback")` 하나와 그 "규칙"
하나를 만든다. 규칙의 원문은 판단 모델이 쓴 할 일(request)이고, 코멘트의 원문은 위반의 검사 메시지
자리에 자료로 붙인다. 규칙 id는 `feedback-<item_id>`다. 출구 검사는 이 위반을 도구로 다시 확인할
수 없어 그 파일이 바뀌었는지만 본다(model_fix.py).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final

from app.worker.cache_key_pure import text_hash
from app.worker.doc_rules_pure import CallUsage, Found, RuleUnchecked
from app.worker.feedback_pure import Item
from app.worker.feedback_triage_pure import Triage
from app.worker.fix.model_fix_pure import ModelFix
from app.worker.markdown_pure import inline_code
from app.worker.prompt_text_pure import load
from app.worker.rule_pure import CheckMethod, Rule, RuleSource
from app.worker.violation_pure import Violation, violation_id

RULE_PREFIX: Final = "feedback-"
SOURCE_FILE: Final = "PR 코멘트"
MAX_MESSAGE_CHARS: Final = 600

# 수정 작성의 시스템 프롬프트 뒤에 붙인다. 저장 키에도 들어간다
FIX_NOTE: Final = "\n\n" + load("model_fix.feedback")


@dataclass(frozen=True)
class Accepted:
    item: Item
    triage: Triage


def rule_id(item: Item) -> str:
    return f"{RULE_PREFIX}{item.item_id}"


def _one_line(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def fix_targets(
    accepted: Sequence[Accepted], lines_of: Mapping[str, Sequence[str]]
) -> tuple[list[Violation], list[Rule]]:
    """받아들인 코멘트의 대상 위반과 규칙. lines_of는 파일 → 지금 줄들(고치기 전)이다.

    코멘트가 달린 파일의 위반은 코멘트의 줄, 그 밖의 파일은 줄 0(파일 전체)이다. 위반 id에 할 일의
    해시를 넣어, 같은 코멘트라도 할 일이 바뀌면 수정 작성의 저장 키가 달라진다.
    """
    violations, rules = [], []
    for a in accepted:
        item, request = a.item, a.triage.request
        rid = rule_id(item)
        rules.append(Rule(
            id=rid, text=request,
            source=RuleSource(SOURCE_FILE, f"{item.kind} {item.item_id}", ""),
            origin="document", scope=(), check=CheckMethod("none", reason=SOURCE_FILE),
            fix="model",
        ))  # fmt: skip
        message = f"코멘트 원문: {_one_line(item.body, MAX_MESSAGE_CHARS)}"
        for file in a.triage.files:
            lines = lines_of.get(file, ())
            line = item.line if file == item.path and item.line else 0
            text = lines[line - 1].strip() if 0 < line <= len(lines) else ""
            violations.append(Violation(
                id=violation_id(f"{rid}:{text_hash(request)[:12]}", file, text, 0),
                rule_id=rid, file=file, line=line, message=message, found_by="feedback",
                line_text=text,
            ))  # fmt: skip
    return violations, rules


def feedback_found(
    base: Found | None, violations: Sequence[Violation], rules: Sequence[Rule]
) -> Found:
    """수정 작성에 넘기는 위반 전체. base는 이 커밋의 규칙 검사 결과(없으면 None)다. 그 위반과
    규칙을 같이 넣어야 출구 검사가 있던 위반을 새 위반으로 잘못 세지 않는다."""
    outcomes = tuple(RuleUnchecked(r, "no_checker", SOURCE_FILE) for r in rules)
    if base is None:
        return Found(outcomes, tuple(violations), 0, CallUsage(0, 0, 0, (), 0, 0))
    return replace(
        base, outcomes=(*base.outcomes, *outcomes), violations=(*base.violations, *violations)
    )


def proposal_title(pr_number: int, count: int) -> str:
    return f"[pipeline] #{pr_number} 리뷰 코멘트 {count}건 반영"


def comment_lines(accepted: Sequence[Accepted], fix: ModelFix) -> list[str]:
    """수정안 PR 본문의 "반영한 코멘트": 코멘트마다 글쓴이, 파일·줄, 할 일, 모델의 설명. 사람과
    모델의 글은 코드 스팬 안에만 둔다."""
    why: dict[str, str] = {}
    if fix.explanation is not None:
        for c in fix.explanation.changes:
            why.setdefault(c.rule, c.why)
    out = []
    for a in accepted:
        item = a.item
        where = f"{item.path}:{item.line}" if item.path and item.line else ", ".join(a.triage.files)
        out.append(f"- {inline_code(item.login, 100)} · {inline_code(where, 200)} "
                   f"(코멘트 id {item.item_id})")  # fmt: skip
        out.append(f"  - 코멘트: {inline_code(item.body, 300)}")
        out.append(f"  - 할 일 (파이프라인의 판단): {inline_code(a.triage.request, 300)}")
        if rule_id(item) in why:
            out.append(f"  - 고친 것 (모델의 설명): {inline_code(why[rule_id(item)], 300)}")
    return out
