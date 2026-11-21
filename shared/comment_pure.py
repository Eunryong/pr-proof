"""파이프라인 PR 코멘트의 공용 표시.

파이프라인은 사용자의 gh 계정으로 코멘트를 달므로, 작성자가 아니라 이 표시로 자기 코멘트를 알아본다.
웹은 이 표시가 있는 코멘트를 명령으로 받지 않고, 워커는 이 표시로 고칠 코멘트를 찾는다.
명령으로 받을지는 command_in_comment() 하나로 정한다 (웹훅·동기화 공용).
"""

from typing import Final

COMMENT_MARKER: Final = "<!-- pr-proof -->"
# 명령에 답하는 코멘트의 표시. 결과 코멘트를 찾을 때 섞이지 않도록 COMMENT_MARKER를 포함하지 않는다
REPLY_MARKER: Final = "<!-- pr-proof:reply -->"
# 명령을 받을 수 있는 작성자 (GitHub author_association)
TRUSTED_AUTHORS: Final = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


def command_in_comment(body: str | None, author_association: str | None) -> tuple[str | None, str]:
    """코멘트가 처리할 명령이면 (명령 줄, ""), 아니면 (None, 무시 이유).

    웹훅과 동기화가 같은 함수로 거른다. 둘 중 한쪽만 권한을 확인하면 그쪽이 구멍이 된다.
    """
    text = (body or "").strip()
    if COMMENT_MARKER in text or REPLY_MARKER in text:
        return None, "파이프라인 자신의 코멘트"
    if not text.startswith("/"):
        return None, "명령이 아닌 코멘트"
    if author_association not in TRUSTED_AUTHORS:
        return None, "명령 권한 없음"
    return text.splitlines()[0], ""
