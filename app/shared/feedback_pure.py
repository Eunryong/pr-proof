"""PR에 달린 코멘트(Copilot 리뷰, 사람의 리뷰)를 다룰지 정하는 공용 규칙.

파이프라인은 사용자의 gh 계정으로 글을 달므로 계정 이름으로는 자기 글을 가릴 수 없고, 표시
(`<!-- pr-proof` 로 시작하는 세 가지)로만 가린다. 웹훅과 동기화와 워커가 `feedback_in_comment()`
하나로 거른다(`command_in_comment`와 같은 원칙). 한쪽만 거르면 그쪽이 구멍이 되고, 파이프라인의
답글이 다시 이벤트를 일으켜 도는 일이 생긴다.
"""

import re
from typing import Final, Literal

from app.shared.comment_pure import TRUSTED_AUTHORS

# 파이프라인이 코멘트에 답하는 글의 표시. 다른 두 표시와 같이 `<!-- pr-proof`로 시작한다
FEEDBACK_MARKER: Final = "<!-- pr-proof:feedback -->"
# 세 표시(결과 코멘트, 명령 답글, 코멘트 답글)가 모두 이 글자로 시작한다
PIPELINE_MARKER_PREFIX: Final = "<!-- pr-proof"
# Copilot 리뷰의 글쓴이. REST는 "Copilot"이나 "copilot-pull-request-reviewer[bot]", GraphQL은
# "copilot-pull-request-reviewer"로 준다. author_association은 NONE이라 로그인으로 가린다
COPILOT_LOGINS: Final = frozenset(
    {"Copilot", "copilot-pull-request-reviewer[bot]", "copilot-pull-request-reviewer"}
)

# review_comment: 줄 리뷰 코멘트, review: 리뷰 본문, issue_comment: PR의 일반 코멘트
FeedbackKind = Literal["review_comment", "review", "issue_comment"]

_FIX_BRANCH: Final = re.compile(r"pipeline/pr-(\d+)-fix-[^/\s]+")
# PR에 새 커밋이 오면 다시 보는 기록: 받아들였지만 지금은 고칠 수 없었음, 처리하지 못함
REVISIT_ON_NEW_HEAD: Final = frozenset({"deferred", "error"})


def handled(verdict: str, handled_head: str, head_sha: str, superseded: bool = False) -> bool:
    """기록이 있는 코멘트를 처리한 것으로 볼지. 워커의 `collect`와 동기화가 같이 쓴다.

    deferred·error는 그 기록을 남긴 커밋이 지금 head일 때만 처리한 것이다. 새 커밋에서는 다시 본다
    (테스트가 고쳐졌거나, 열린 수정안이 닫혔을 수 있다). 받아들여 연 수정안이 새 커밋으로 닫혔으면
    (superseded) 그 코멘트도 새 커밋에서 다시 본다.
    """
    if handled_head == head_sha:
        return True
    return verdict not in REVISIT_ON_NEW_HEAD and not superseded


def feedback_in_comment(
    body: str | None, author_association: str | None, login: str | None
) -> tuple[bool, str]:
    """코멘트가 다룰 글이면 (True, ""), 아니면 (False, 이유).

    파이프라인 자신의 글과 `/` 명령은 뺀다(명령은 `command_in_comment`가 다룬다). 글쓴이는 Copilot
    이거나 저장소의 소유자·멤버·협업자여야 한다.
    """
    text = (body or "").strip()
    if not text:
        return False, "본문이 빈 코멘트"
    if PIPELINE_MARKER_PREFIX in text:
        return False, "파이프라인 자신의 코멘트"
    if text.startswith("/"):
        return False, "명령"
    if login not in COPILOT_LOGINS and author_association not in TRUSTED_AUTHORS:
        return False, "코멘트 권한 없음"
    return True, ""


def fix_branch_pr(ref: str) -> int | None:
    """`pipeline/pr-<n>-fix-<묶음>` 꼴의 브랜치 이름에서 원래 PR 번호 n. 꼴이 다르면 None."""
    match = _FIX_BRANCH.fullmatch(ref)
    return int(match.group(1)) if match else None


ResolutionReason = Literal["ADDRESSED", "WONT_FIX", "INVALID"]


def resolution_reason(login: str, *, rejected: bool = False, incorrect: bool = False
                      ) -> ResolutionReason | None:  # fmt: skip
    """Copilot 리뷰 스레드를 해결할 때 GitHub에 주는 사유. 사람의 스레드면 None(사유를 받지 않는다).

    고쳤거나 이미 반영됐으면 ADDRESSED, 거절이면 WONT_FIX, 코멘트의 말이 사실이 아니어서 거절이면
    INVALID다(화면의 Addressed / Won't fix / Incorrect).
    """
    if login not in COPILOT_LOGINS:
        return None
    if not rejected:
        return "ADDRESSED"
    return "INVALID" if incorrect else "WONT_FIX"
