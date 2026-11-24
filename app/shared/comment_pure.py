"""파이프라인 PR 코멘트의 공용 표시.

파이프라인은 사용자의 gh 계정으로 코멘트를 달므로, 작성자가 아니라 이 표시로 자기 코멘트를 알아본다.
웹은 이 표시가 있는 코멘트를 명령으로 받지 않고, 워커는 이 표시로 고칠 코멘트를 찾는다.
명령으로 받을지는 command_in_comment() 하나로 정한다 (웹훅·동기화 공용).

결과 코멘트의 첫 줄은 `result_marker`가 만든다. `COMMENT_MARKER` 뒤에 그 코멘트를 쓴 서버의 id와
판, 코멘트가 말하는 커밋을 담은 HTML 주석을 하나 더 붙인다. 앞부분이 `COMMENT_MARKER` 그대로라
`COMMENT_MARKER`로 찾는 코드(이 기능이 없는 판의 서버 포함)도 같은 코멘트를 찾는다.
"""

import re
from typing import Final

from app.shared.other_server_pure import Owner

COMMENT_MARKER: Final = "<!-- pr-proof -->"
# 결과 코멘트를 쓴 서버의 표시. 사람 눈에 보이지 않는 HTML 주석이다
_OWNER: Final = re.compile(
    r"<!-- pr-proof:server id=([0-9a-f]{8,32}) version=([0-9A-Za-z.+-]{0,64}) "
    r"head=([0-9a-f]{40}) -->"
)
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


def result_marker(server_id: str, version: str, head_sha: str) -> str:
    """결과 코멘트의 첫 줄. 판에 표시의 꼴을 깨는 글자가 있으면 판을 비운다(읽는 쪽은 "같음")."""
    if not re.fullmatch(r"[0-9A-Za-z.+-]{0,64}", version):
        version = ""
    return (
        f"{COMMENT_MARKER}<!-- pr-proof:server id={server_id} version={version} head={head_sha} -->"
    )


def read_owner(text: str) -> Owner | None:
    """결과 코멘트(또는 그 첫 줄)에서 쓴 서버를 읽는다. 서버 정보가 없는 옛 표시이거나 꼴이 맞지
    않으면 None(주인을 모름)이다."""
    if COMMENT_MARKER not in text:
        return None
    m = _OWNER.search(text)
    if m is None:
        return None
    return Owner(server_id=m.group(1), version=m.group(2), head_sha=m.group(3))
