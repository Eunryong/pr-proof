"""결과 코멘트의 이슈 절 (docs/DESIGN.md 4.5 끝 "연결된 이슈 확인", 4.9). 판정을 새로 내지 않는다.

`LinkedIssues` 하나만 받는다. 맨 위 결론 줄 하나("**이슈:**", "**PR 설명:**" 줄 아래)와, 펼친
부분에는 사람이 할 일이 있는 것(빠진 것, 코드로 확인할 수 없는 요구, 견주지 못한 까닭, 보지 않은
이슈, 잘라서 일부만 견준 것)만, 접는 블록에는 사용량을 둔다. 기계적인 분류 이름을 쓰지 않고 리뷰어가
말하듯 적는다. 모델이 쓴 글과 저장소 이름은 믿지 않는 글이라 코드 스팬 안에만 넣는다
(app/worker/core/markdown_pure.py). 이슈 번호는 이 저장소의 GitHub 응답에서 온 정수다.
"""

from collections.abc import Mapping, Sequence
from typing import Final

from app.worker.core.markdown_pure import inline_code
from app.worker.report.pr_description_report_pure import large_change_notes
from app.worker.rules.linked_issues_pure import MAX_ISSUES, IssueGap, LinkedIssues

TITLE: Final = "**이슈:**"
NONE: Final = f"{TITLE} 연결된 이슈가 없습니다."
LABEL: Final[Mapping[str, str]] = {
    "missing": "이슈가 요구했는데 빠짐",
    "unverifiable": "코드로 확인할 수 없음",
}
_GAP_TEXT: Final[Mapping[str, str]] = {
    "cap": "모델 호출 상한에 걸림",
    "model_failed": "모델 호출이 실패함",
    "unreadable": "이슈를 읽지 못함",
    "other_repo": "다른 저장소의 이슈만 연결됨",
    "no_diff": "바뀐 내용이 없음",
}
MAX_SUMMARY_CHARS: Final = 300
MAX_DETAIL_CHARS: Final = 300
MAX_OTHER_SHOWN: Final = 10
# 이슈의 수정 소식은 받지 않는다 (4.5 끝)
RECHECK_HINT: Final = (
    "이슈 글을 고쳤으면 `/recheck`를 달아 다시 견주세요. 이슈만 고친 것은 저절로 다시 보지 "
    "않습니다."
)


def _subject(numbers: Sequence[int]) -> str:
    """ "#73이", "#73, #75가". 조사는 마지막 번호를 읽은 소리의 받침으로 고른다(2·4·5·9로 끝나면
    받침이 없다: 이, 사, 오, 구)."""
    names = ", ".join(f"#{n}" for n in numbers)
    return names + ("가" if numbers and numbers[-1] % 10 in (2, 4, 5, 9) else "이")


def title(d: LinkedIssues | None) -> str | None:
    """맨 위 결론 줄. 이 확인을 끈 저장소나 돌기 전에 끝난 검사는 None."""
    if d is None:
        return None
    match d.status:
        case "none":
            return NONE
        case "unchecked":
            return f"{TITLE} 견주지 못했습니다 ({_GAP_TEXT.get(d.gap, '까닭을 모름')})."
        case _:
            missing = d.missing
            if not missing:
                return f"{TITLE} {_subject(d.issues)} 요구한 것을 해결했습니다."
            return f"{TITLE} {_subject(d.issues)} 요구한 것 중 빠진 곳이 {len(missing)}건 있습니다."


def expanded(d: LinkedIssues | None) -> list[str]:
    """펼친 부분. 사람이 할 일이 없으면 빈 목록이다. 해결된 요구는 나열하지 않는다."""
    if d is None or d.status == "none":
        return []
    lines: list[str] = []
    if d.status == "unchecked":
        if d.detail:
            lines += ["", f"이슈와 견주지 못한 까닭: {inline_code(d.detail, MAX_DETAIL_CHARS)}"]
        if d.gap in ("cap", "model_failed"):
            lines.append("다음 커밋의 검사나 `/recheck`가 다시 견줍니다.")
    if d.items:
        ordered = [*d.missing, *(g for g in d.items if g.kind != "missing")]
        lines += ["", *(_item(g) for g in ordered)]
        if d.missing:
            lines.append(RECHECK_HINT)
    others = [f"- 다른 저장소의 이슈라 보지 않음: {inline_code(name, 200)}"
              for name in d.other_repo[:MAX_OTHER_SHOWN]]  # fmt: skip
    if len(d.other_repo) > MAX_OTHER_SHOWN:
        others.append(f"- 다른 저장소의 이슈 외 {len(d.other_repo) - MAX_OTHER_SHOWN}개")
    if others:
        lines += ["", *others]
    notes = []
    if d.over:
        notes.append(
            f"연결된 이슈가 많아 앞의 {MAX_ISSUES}개만 견줬습니다 (보지 않은 이슈 {d.over}개)."
        )
    if d.unreadable and d.status != "unchecked":
        notes.append(f"읽지 못한 연결된 이슈 {d.unreadable}개는 보지 않았습니다.")
    notes += large_change_notes(d.files, d.shown, d.batches, d.left)
    if d.text_cut:
        notes.append("이슈 글이나 댓글이 길어 일부만 견줬습니다.")
    if notes:
        lines += ["", " ".join(notes)]
    return lines


def folded(d: LinkedIssues | None) -> list[str]:
    """접는 블록에 넣는 사용량 줄. 모델을 부르지도 저장된 답을 쓰지도 않았으면 빈 목록이다."""
    if d is None:
        return []
    usage = d.usage
    if not usage.calls and not usage.cached:
        return []
    parts = [f"모델 호출 {usage.calls}회"]
    if usage.calls:
        parts.append(f"읽기 도구 {usage.tool_uses}회")
    parts.append(f"저장된 결과 {usage.cached}건 사용")
    if usage.models:
        parts.append(", ".join(inline_code(m, 80) for m in usage.models))
    if usage.dropped:
        parts.append(f"버린 답 {usage.dropped}건 (이슈 글을 그대로 인용하지 않음)")
    if usage.calls:
        parts.append(f"토큰 입력 {usage.input_tokens}, 출력 {usage.output_tokens}")
    if usage.earlier:
        parts.append(f"지난 결과를 주고 다시 견줌 (지난 항목 {usage.earlier_items}건 중 유지 "
                     f"{usage.kept_earlier}건)")  # fmt: skip
    if d.hidden:
        parts.append(f"저장소 사람이 아닌 댓글 {d.hidden}개는 읽지 않음")
    return ["", "이슈 확인: " + " · ".join(parts)]


def _item(g: IssueGap) -> str:
    return f"- {LABEL[g.kind]}: {inline_code(g.summary, MAX_SUMMARY_CHARS)} (#{g.issue})"
