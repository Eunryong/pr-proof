"""changes.diff에서 PR이 추가·수정한 줄을 읽는다 (docs/DESIGN.md F6).

위반은 PR이 바꾼 줄에 걸린 것만 다룬다. 기존 코드의 위반은 PR 작성자의 책임이 아니다.
"""

import codecs
import re
from collections.abc import Mapping
from dataclasses import dataclass
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


def hunk_around(diff: str, file: str, line: int, radius: int) -> str:
    """file의 head 쪽 line을 담은 hunk에서 그 줄 앞뒤 radius줄과 hunk 헤더. 없으면 "".

    수정 판단(4.6)이 모델에 넘기는 자료다. 큰 새 파일은 hunk 하나가 파일 전체라 줄 수를 자른다.
    """
    current: str | None = None
    old_left = new_left = 0
    line_no = 0
    header = ""
    hunk: list[tuple[int | None, str]] = []
    for raw in diff.splitlines():
        if old_left > 0 or new_left > 0:
            tag = raw[:1]
            if tag == "-":
                hunk.append((None, raw))
                old_left -= 1
            elif tag == "\\":
                hunk.append((None, raw))
            else:
                hunk.append((line_no, raw))
                line_no += 1
                new_left -= 1
                if tag != "+":
                    old_left -= 1
            if old_left <= 0 and new_left <= 0 and current == file:
                at = next((i for i, (n, _) in enumerate(hunk) if n == line), None)
                if at is not None:
                    window = hunk[max(0, at - radius) : at + radius + 1]
                    return "\n".join([header, *(text for _, text in window)])
            continue
        if raw.startswith("diff --git ") or raw == _DELETED:
            current = None
        elif raw.startswith(_NEW_FILE_HEADER):
            current = _new_path(raw)
        elif match := _HUNK.match(raw):
            old_count, new_start, new_count = match.groups()
            old_left = int(old_count) if old_count is not None else 1
            new_left = int(new_count) if new_count is not None else 1
            line_no = int(new_start)
            header, hunk = raw, []
    return ""


@dataclass(frozen=True)
class ChangeBlock:
    """연속으로 바뀐 줄 한 덩어리 (`-U0`의 hunk 하나와 같다). lines는 이 덩어리를 가리킨다고 보는
    head 쪽 줄 번호다: 더한 줄, 지우기만 했으면 지운 자리의 앞뒤 줄."""

    file: str
    lines: frozenset[int]


def change_blocks(diff: str) -> tuple[ChangeBlock, ...]:
    """diff의 바뀐 덩어리들. 문맥 줄이 있는 diff도 문맥 줄에서 덩어리를 나눈다. 지운 파일은 뺀다.

    수정의 설명(DESIGN.md 7.5)이 바뀐 줄을 가리키는지 확인하는 데 쓴다.
    """
    blocks: list[ChangeBlock] = []
    current: str | None = None
    old_left = new_left = 0
    line_no = 0
    added: set[int] | None = None  # 지금 덩어리에서 더한 줄. 덩어리 밖이면 None

    def close() -> None:
        nonlocal added
        if added is not None and current is not None:
            lines = added or {n for n in (line_no - 1, line_no) if n >= 1}
            blocks.append(ChangeBlock(current, frozenset(lines)))
        added = None

    for raw in diff.splitlines():
        if old_left > 0 or new_left > 0:
            tag = raw[:1]
            if tag == "+":
                added = set() if added is None else added
                added.add(line_no)
                line_no += 1
                new_left -= 1
            elif tag == "-":
                added = set() if added is None else added
                old_left -= 1
            elif tag != "\\":
                close()
                line_no += 1
                old_left -= 1
                new_left -= 1
            if old_left <= 0 and new_left <= 0:
                close()
            continue
        if raw.startswith("diff --git ") or raw == _DELETED:
            current = None
        elif raw.startswith(_NEW_FILE_HEADER):
            current = _new_path(raw)
        elif match := _HUNK.match(raw):
            old_count, new_start, new_count = match.groups()
            old_left = int(old_count) if old_count is not None else 1
            new_left = int(new_count) if new_count is not None else 1
            # 지우기만 한 hunk의 시작 줄은 지운 자리의 앞줄이다
            line_no = int(new_start) + (1 if new_left == 0 else 0)
    return tuple(blocks)


def hunk_removing(diff: str, file: str, line_text: str, max_lines: int) -> str:
    """file의 hunk 중 앞뒤 공백을 뺀 내용이 line_text인 줄을 지운(바꾼) 것. 없으면 그 파일의 첫
    hunk, 그 파일이 diff에 없으면 "". hunk 헤더를 포함해 max_lines줄까지 자른다.

    과거 기록(6.1)에 넣는 "관련 hunk"다. 위반 줄은 고친 쪽에서 지워진 줄로 보인다.
    """
    wanted = line_text.strip()
    current: str | None = None
    old_left = new_left = 0
    first: list[str] | None = None
    hunk: list[str] = []
    hit = False
    for raw in diff.splitlines():
        if old_left > 0 or new_left > 0:
            hunk.append(raw)
            tag = raw[:1]
            if tag == "-":
                old_left -= 1
                hit = hit or raw[1:].strip() == wanted
            elif tag == "+":
                new_left -= 1
            elif tag != "\\":
                old_left -= 1
                new_left -= 1
            if old_left <= 0 and new_left <= 0 and current == file:
                if hit:
                    return "\n".join(hunk[:max_lines])
                first = hunk if first is None else first
            continue
        if raw.startswith("diff --git ") or raw == _DELETED:
            current = None
        elif raw.startswith(_NEW_FILE_HEADER):
            current = _new_path(raw)
        elif match := _HUNK.match(raw):
            old_count, _, new_count = match.groups()
            old_left = int(old_count) if old_count is not None else 1
            new_left = int(new_count) if new_count is not None else 1
            hunk, hit = [raw], False
    return "\n".join(first[:max_lines]) if first is not None else ""


def touches(changed: ChangedLines, file: str, start: int, end: int) -> bool:
    """file의 start–end 줄 범위가 PR이 바꾼 줄을 하나라도 포함하는지.

    import 정렬처럼 여러 줄에 걸친 위반은 범위 안의 한 줄만 바뀌어도 PR의 위반으로 본다.
    """
    lines = changed.get(file)
    if not lines:
        return False
    return any(n in lines for n in range(start, max(start, end) + 1))
