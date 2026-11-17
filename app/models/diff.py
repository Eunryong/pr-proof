"""changes.diff에서 PR이 추가·수정한 줄을 읽는다 (docs/DESIGN.md F6).

위반은 PR이 바꾼 줄에 걸린 것만 다룬다. 기존 코드의 위반은 PR 작성자의 책임이 아니다.
"""

import re
from collections.abc import Mapping
from typing import Final

_HUNK: Final = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_NEW_FILE_PREFIX: Final = "+++ b/"
_DELETED: Final = "+++ /dev/null"

ChangedLines = Mapping[str, frozenset[int]]


def added_lines(diff: str) -> ChangedLines:
    """파일(리포 기준 경로)별로 head에서 추가·수정된 줄 번호. 삭제된 파일은 들어가지 않는다.

    hunk 안에서는 헤더의 줄 수만큼 내용 줄로 읽는다. 추가된 내용이 `++`로 시작하면
    diff에는 `+++`로 보이므로, 줄 수를 세지 않으면 파일 헤더로 잘못 읽는다.
    """
    result: dict[str, set[int]] = {}
    current: str | None = None
    old_left = new_left = 0
    line_no = 0
    for raw in diff.splitlines():
        if old_left > 0 or new_left > 0:
            tag = raw[:1]
            if tag == "+":
                if current is not None:
                    result.setdefault(current, set()).add(line_no)
                line_no += 1
                new_left -= 1
            elif tag == "-":
                old_left -= 1
            elif tag == "\\":
                pass  # "\ No newline at end of file"
            else:  # 문맥 줄 (빈 줄은 공백이 지워진 문맥 줄로 본다)
                line_no += 1
                old_left -= 1
                new_left -= 1
            continue
        if raw.startswith("diff --git "):
            current = None
        elif raw == _DELETED:
            current = None
        elif raw.startswith(_NEW_FILE_PREFIX):
            current = raw[len(_NEW_FILE_PREFIX) :]
        elif match := _HUNK.match(raw):
            old_count, new_start, new_count = match.groups()
            old_left = int(old_count) if old_count is not None else 1
            new_left = int(new_count) if new_count is not None else 1
            line_no = int(new_start)
    return {path: frozenset(lines) for path, lines in result.items()}


def touches(changed: ChangedLines, file: str, start: int, end: int) -> bool:
    """file의 start–end 줄 범위가 PR이 바꾼 줄을 하나라도 포함하는지.

    import 정렬처럼 여러 줄에 걸친 위반은 범위 안의 한 줄만 바뀌어도 PR의 위반으로 본다.
    """
    lines = changed.get(file)
    if not lines:
        return False
    return any(n in lines for n in range(start, max(start, end) + 1))
