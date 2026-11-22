"""결과 코멘트의 PR 설명 절 (docs/DESIGN.md 4.5 끝 "PR 설명과 diff 견주기", 4.9). 판정을 새로
내지 않는다.

`PrDescription` 하나만 받는다. 맨 위 결론 줄 하나("**PR 설명:**")와, 펼친 부분에는 사람이 할 일이
있는 것(어긋난 곳, 견주지 못한 까닭, 잘라서 일부만 견준 것)만, 접는 블록에는 사용량을 둔다. 기계적인
분류 이름을 쓰지 않고 리뷰어가 말하듯 적는다. 모델이 쓴 글과 파일 경로는 믿지 않는 글이라 코드 스팬
안에만 넣는다 (app/worker/markdown_pure.py).
"""

from collections.abc import Mapping
from typing import Final

from app.worker.markdown_pure import inline_code
from app.worker.rules.pr_description_pure import Mismatch, PrDescription

TITLE: Final = "**PR 설명:**"
MATCHED: Final = f"{TITLE} 설명한 내용과 변경이 맞습니다."
EMPTY: Final = f"{TITLE} 설명이 비어 있어 견주지 못했습니다."
HEAD: Final = "**PR 설명과 다른 곳:**"
LABEL: Final[Mapping[str, str]] = {
    "unrelated": "PR과 관련 없는 수정",
    "missing": "설명에는 있는데 코드에 없음",
    "different": "설명과 다름",
}
_GAP_TEXT: Final[Mapping[str, str]] = {
    "cap": "모델 호출 상한에 걸림",
    "model_failed": "모델 호출이 실패함",
    "no_facts": "PR 글을 읽지 못함",
    "no_diff": "바뀐 내용이 없음",
}
MAX_SUMMARY_CHARS: Final = 300
MAX_DETAIL_CHARS: Final = 300
# PR 제목·본문만 고친 것(pull_request.edited)은 다시 보지 않는다 (4.5 끝)
RECHECK_HINT: Final = (
    "PR 제목·본문을 고쳤으면 `/recheck`를 달아 다시 견주세요. 커밋 없이 고친 것은 저절로 다시 "
    "보지 않습니다."
)
EMPTY_HINT: Final = "PR 본문이나 커밋 메시지에 무엇을 바꿨는지 적고 `/recheck`를 달면 견줍니다."


def title(d: PrDescription | None) -> str | None:
    """맨 위 결론 줄. 이 확인을 끈 저장소나 돌기 전에 끝난 검사는 None."""
    if d is None:
        return None
    match d.status:
        case "empty":
            return EMPTY
        case "unchecked":
            return f"{TITLE} 견주지 못했습니다 ({_GAP_TEXT.get(d.gap, '까닭을 모름')})."
        case _:
            if not d.items:
                return MATCHED
            return f"{TITLE} 설명과 다른 곳이 {len(d.items)}건 있습니다."


def expanded(d: PrDescription | None) -> list[str]:
    """펼친 부분. 사람이 할 일이 없으면 빈 목록이다. 맞게 된 항목은 나열하지 않는다."""
    if d is None:
        return []
    lines: list[str] = []
    if d.status == "empty":
        return ["", EMPTY_HINT]
    if d.status == "unchecked":
        if d.detail:
            lines += ["", f"PR 설명을 견주지 못한 까닭: {inline_code(d.detail, MAX_DETAIL_CHARS)}"]
        if d.gap in ("cap", "model_failed"):
            lines.append("다음 커밋의 검사나 `/recheck`가 다시 견줍니다.")
        return lines
    if d.items:
        lines += ["", HEAD, *(_item(m) for m in d.items), RECHECK_HINT]
    notes = []
    if d.diff_cut:
        notes.append(
            f"변경이 커서 바뀐 파일 {d.files}개 중 {d.files - d.shown}개는 diff를 다 보이지 "
            "못했습니다. 그 파일은 모델이 필요할 때 작업 폴더에서 읽었습니다."
        )
    if d.text_cut:
        notes.append("PR 글이 길어 앞부분만 견줬습니다.")
    if notes:
        lines += ["", " ".join(notes)]
    return lines


def folded(d: PrDescription | None) -> list[str]:
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
        parts.append(f"버린 답 {usage.dropped}건 (PR 글이나 바뀐 줄을 그대로 가리키지 않음)")
    if usage.calls:
        parts.append(f"토큰 입력 {usage.input_tokens}, 출력 {usage.output_tokens}")
    return ["", "PR 설명 확인: " + " · ".join(parts)]


def _item(m: Mismatch) -> str:
    line = f"- {LABEL[m.kind]}: {inline_code(m.summary, MAX_SUMMARY_CHARS)}"
    if m.file:
        place = inline_code(f"{m.file}:{m.line}", 200)
        line += f" ({place}{', 지운 줄' if m.side == 'old' else ''})"
    return line
