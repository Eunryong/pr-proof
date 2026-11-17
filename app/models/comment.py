"""파이프라인 PR 코멘트의 공용 표시.

파이프라인은 사용자의 gh 계정으로 코멘트를 달므로, 작성자가 아니라 이 표시로 자기 코멘트를 알아본다.
웹은 이 표시가 있는 코멘트를 명령으로 받지 않고, 워커는 이 표시로 고칠 코멘트를 찾는다.
"""

from typing import Final

COMMENT_MARKER: Final = "<!-- pr-proof -->"
# 명령에 답하는 코멘트의 표시. 결과 코멘트를 찾을 때 섞이지 않도록 COMMENT_MARKER를 포함하지 않는다
REPLY_MARKER: Final = "<!-- pr-proof:reply -->"
