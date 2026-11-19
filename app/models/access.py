"""PR 작성자가 신뢰되는지 (docs/DESIGN.md 8.6, 위협 표 T2).

임시 가드레일이다. 신뢰되지 않은 사람이 연 PR에는 아무것도 돌리지 않는다. 승인(`/recheck <커밋>`),
저장소별 설정(`access.trusted`, `access.bots`)은 6단계의 정식 가드레일에서 넣는다. 그때까지는
승인할 방법이 없고, 봇도 신뢰되지 않는다.
"""

from typing import Final

# GitHub의 author_association 중 저장소에 쓸 수 있는 사람의 값. 허용 목록 방식이라 GitHub이 새
# 값을 더해도 막는 쪽으로 간다
TRUSTED_ASSOCIATIONS: Final = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


def may_run(author_association: str | None) -> bool:
    """워커가 GitHub에서 방금 읽은 값으로만 부른다. 없거나 모르는 값이면 False."""
    return author_association in TRUSTED_ASSOCIATIONS
