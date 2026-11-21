"""ruff가 낸 수정 편집을 텍스트에 적용한다 (docs/DESIGN.md 4.7 기계적 수정).

`ruff check --fix`는 파일 전체의 위반을 고쳐 PR이 건드리지 않은 줄까지 바꾼다. 그래서 ruff의
JSON 출력에 든 편집 중 수정 판단이 고치기로 정한 위반의 것만 골라 직접 적용한다. 편집은 텍스트
치환이라 코드를 실행하지 않는다.
"""

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class TextEdit:
    """(row, column)부터 (end_row, end_column) 앞까지를 content로 바꾼다.

    줄·열은 1부터 세고, 열은 문자 단위다.
    """

    row: int
    column: int
    end_row: int
    end_column: int
    content: str


def _line_starts(text: str) -> list[int]:
    starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def _offset(starts: list[int], text: str, row: int, column: int) -> int:
    if row > len(starts):  # 파일 끝 다음 줄을 가리키는 끝 위치
        return len(text)
    return min(starts[row - 1] + column - 1, len(text))


def apply_edits(text: str, groups: Sequence[Sequence[TextEdit]]) -> tuple[str, int]:
    """위반별 편집 묶음을 적용한다. 앞서 고른 묶음과 겹치는 묶음은 건너뛴다.

    (새 텍스트, 적용한 묶음 수). 겹쳐서 건너뛴 위반은 다음 회차에 ruff를 다시 돌려 고친다
    (ruff --fix도 같은 방식으로 여러 번 돈다).
    """
    starts = _line_starts(text)
    chosen: list[tuple[int, int, str]] = []
    applied = 0
    for group in groups:
        spans = [
            (_offset(starts, text, e.row, e.column), _offset(starts, text, e.end_row, e.end_column),
             e.content)
            for e in group
        ]  # fmt: skip
        if not spans or any(_overlaps(s, c) for s in spans for c in chosen):
            continue
        chosen.extend(spans)
        applied += 1
    for start, end, content in sorted(chosen, key=lambda span: span[0], reverse=True):
        text = text[:start] + content + text[end:]
    return text, applied


def _overlaps(a: tuple[int, int, str], b: tuple[int, int, str]) -> bool:
    # 같은 위치에 넣기만 하는 두 편집도 순서가 정해지지 않으므로 겹친 것으로 본다
    return (a[0] < b[1] and b[0] < a[1]) or a[0] == b[0]
