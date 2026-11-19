"""changes.diff에서 PR이 추가·수정한 줄을 읽는다 (docs/DESIGN.md F6).

위반은 PR이 바꾼 줄에 걸린 것만 다룬다. 기존 코드의 위반은 PR 작성자의 책임이 아니다.
"""

import codecs
import re
from collections.abc import Mapping
from typing import Final

_HUNK: Final = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_NEW_FILE_HEADER: Final = "+++ "
_DELETED: Final = "+++ /dev/null"

ChangedLines = Mapping[str, frozenset[int]]


def _new_path(header: str) -> str | None:
    """`+++ b/<경로>` 헤더의 경로. 형식이 다르면 None.

    git은 공백이 든 경로 뒤에 탭을 붙이고, 특수 문자가 든 경로는 `"b/\\353..."`처럼 C 형식으로
    감싼다 (core.quotePath). 그대로 읽으면 그 파일의 바뀐 줄이 빠져 위반이 보이지 않는다.
    """
    raw = header[len(_NEW_FILE_HEADER) :].removesuffix("\t")
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        raw = codecs.escape_decode(raw[1:-1].encode())[0].decode("utf-8", "surrogateescape")
    return raw.removeprefix("b/") if raw.startswith("b/") else None


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
        elif raw.startswith(_NEW_FILE_HEADER):
            current = _new_path(raw)
        elif match := _HUNK.match(raw):
            old_count, new_start, new_count = match.groups()
            old_left = int(old_count) if old_count is not None else 1
            new_left = int(new_count) if new_count is not None else 1
            line_no = int(new_start)
    return {path: frozenset(lines) for path, lines in result.items()}


def new_files(diff: str) -> frozenset[str]:
    """PR이 새로 만들거나 이름을 바꿔 생긴 파일(리포 기준 경로). 경로 규칙은 이 파일만 본다:
    원래 있던 파일의 이름은 PR 작성자의 책임이 아니다.

    hunk 안의 줄은 건너뛴다 (내용이 헤더처럼 보일 수 있다). 빈 새 파일은 `+++` 줄이 없어
    `diff --git` 줄에서 경로를 읽는다.
    """
    result: set[str] = set()
    git_path: str | None = None
    is_new = False
    old_left = new_left = 0
    for raw in diff.splitlines():
        if old_left > 0 or new_left > 0:
            tag = raw[:1]
            if tag == "+":
                new_left -= 1
            elif tag == "-":
                old_left -= 1
            elif tag != "\\":
                old_left -= 1
                new_left -= 1
            continue
        if raw.startswith("diff --git "):
            git_path, is_new = _same_path(raw[len("diff --git ") :]), False
        elif raw.startswith("new file mode "):
            is_new = True
            if git_path is not None:
                result.add(git_path)
        elif raw.startswith("rename to "):
            result.add(_unquoted(raw[len("rename to ") :]))
        elif raw.startswith(_NEW_FILE_HEADER) and raw != _DELETED:
            path = _new_path(raw)
            if is_new and path is not None:
                result.add(path)
        elif match := _HUNK.match(raw):
            old_count, _, new_count = match.groups()
            old_left = int(old_count) if old_count is not None else 1
            new_left = int(new_count) if new_count is not None else 1
    return frozenset(result)


def _same_path(rest: str) -> str | None:
    """`a/<경로> b/<경로>`에서 두 경로가 같으면 그 경로 (새 파일은 같다). 아니면 None."""
    size = (len(rest) - 5) // 2
    path = rest[2 : 2 + size]
    return path if size > 0 and rest == f"a/{path} b/{path}" else None


def _unquoted(raw: str) -> str:
    raw = raw.removesuffix("\t")
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        return codecs.escape_decode(raw[1:-1].encode())[0].decode("utf-8", "surrogateescape")
    return raw


def touches(changed: ChangedLines, file: str, start: int, end: int) -> bool:
    """file의 start–end 줄 범위가 PR이 바꾼 줄을 하나라도 포함하는지.

    import 정렬처럼 여러 줄에 걸친 위반은 범위 안의 한 줄만 바뀌어도 PR의 위반으로 본다.
    """
    lines = changed.get(file)
    if not lines:
        return False
    return any(n in lines for n in range(start, max(start, end) + 1))
