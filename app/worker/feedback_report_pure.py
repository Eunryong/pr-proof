"""PR 코멘트에 다는 답글과 그 기록 (docs/DESIGN.md 4.11). 입출력이 없다.

답글은 파이프라인이 쓴다. 모델의 글(why)과 실패 이유는 코드 스팬 안에만 넣어 멘션·마크업·명령이
동작하지 않게 한다. 어느 줄도 `/`로 시작하지 않는다(명령으로 읽힌다). 할 일 없음(`no_action`)에는
답글을 달지 않는다. 앞에 붙는 파이프라인 표시(`FEEDBACK_MARKER`)는 github.py가 붙인다.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from app.worker.feedback_pure import Item
from app.worker.markdown_pure import inline_code

# 코멘트 하나를 처리한 결과
# - fixed: 받아들여 이번에 수정안 PR을 염, proposed: 열린 수정안이 이미 고침(머지를 기다림)
# - merged: 머지된 수정안이 고침, already_fixed: 지금 코드에 이미 반영됨 (둘은 스레드를 바로 해결)
# - flagged: 지금 커밋의 규칙 검사가 같은 줄을 위반으로 알림(수정안 없음)
# - rejected, no_action: 모델의 판단. deferred: 받아들였지만 지금은 고칠 수 없음
# - failed: 받아들였지만 검증을 통과한 수정을 만들지 못함, error: 판단하지 못함
Outcome = Literal[
    "fixed", "proposed", "merged", "already_fixed", "flagged", "rejected", "no_action",
    "deferred", "failed", "error",
]  # fmt: skip

VERDICTS: Final[Mapping[str, str]] = {
    "fixed": "accepted", "proposed": "accepted", "merged": "accepted",
    "already_fixed": "accepted", "flagged": "no_action", "rejected": "rejected",
    "no_action": "no_action", "deferred": "deferred", "failed": "error", "error": "error",
}  # fmt: skip
RESOLVES: Final = frozenset({"merged", "already_fixed"})  # 처리하며 스레드를 바로 해결한다
# Copilot의 스레드는 거절도 바로 해결한다(사유 Won't fix / Incorrect). 사람의 스레드는 답글만 달고
# 열어 둔다(리뷰어가 다시 말할 수 있게)
RESOLVES_FOR_COPILOT: Final = RESOLVES | {"rejected"}
FLAGGED_WHY: Final = "규칙 검사가 같은 줄을 이미 위반으로 알림"
MAX_SHOWN: Final = 300


@dataclass(frozen=True)
class Handled:
    """코멘트 하나의 결과. why는 모델의 글, reason은 파이프라인의 글, number는 수정안 PR 번호."""

    outcome: Outcome
    why: str = ""
    reason: str = ""
    number: int | None = None
    incorrect: bool = False  # rejected만: 코멘트의 말이 사실이 아니어서 거절 (해결 사유 INVALID)


def _quoted(label: str, text: str) -> str:
    return f"{label}: {inline_code(text, MAX_SHOWN)}" if text.strip() else ""


def reply_body(item: Item, h: Handled) -> str | None:
    """답글 본문. 답글을 달지 않으면 None (no_action, 판단하지 못함)."""
    why = _quoted("모델 분석 결과", h.why)
    reason = _quoted("이유", h.reason)
    match h.outcome:
        case "fixed":
            lines = [f"반영했습니다. 수정안 #{h.number}", why]
        case "proposed":
            lines = [f"수정안 #{h.number}이 이미 고칩니다. 머지되면 이 스레드를 해결됨으로 "
                     "바꿉니다.", why]  # fmt: skip
        case "merged":
            lines = [f"수정안 #{h.number}에서 고쳤습니다(머지됨)."]
        case "already_fixed":
            lines = ["지금 코드에 이미 반영돼 있습니다.", why]
        case "flagged":
            lines = ["규칙 검사가 같은 줄을 이미 위반으로 알렸습니다(결과 코멘트 참조). "
                     "이 코멘트로 따로 고치지 않습니다."]  # fmt: skip
        case "rejected":
            lines = ["반영하지 않습니다.", why]
        case "deferred":
            lines = ["받아들이지만 지금은 고칠 수 없습니다. 새 커밋이 오면 다시 봅니다.", why,
                     reason]  # fmt: skip
        case "failed":
            lines = ["반영하려 했지만 검증을 통과한 수정을 만들지 못했습니다.", why, reason]
        case _:  # no_action, error
            return None
    if item.kind != "review_comment":  # 스레드가 없어 어느 코멘트에 답하는지 적는다
        kind = "리뷰" if item.kind == "review" else "코멘트"
        lines.insert(0, f"{inline_code(item.login, 100)}의 {kind}(id {item.item_id})에 답합니다.")
    return "\n\n".join(line for line in lines if line)


def record_why(h: Handled) -> str:
    """기록(`feedback.why`)에 남기는 한 줄."""
    if h.outcome == "flagged":
        return FLAGGED_WHY
    return " · ".join(t for t in (h.why, h.reason) if t)
