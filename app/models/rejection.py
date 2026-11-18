"""사람이 닫은 제안 PR을 기억한다 (docs/DESIGN.md F10 거절 기록). 판단만 한다.

제안 PR을 열 때 그 수정이 고친 위반의 (파일, 규칙 코드, 줄 내용)을 남긴다. 사람이 그 제안을
머지하지 않고 닫으면, 다음 커밋에서 같은 파일·규칙의 위반이 같은 줄 내용에 있으면 수정
대상에서 빼고 코멘트에 "이전에 거절됨"으로 적는다. 줄 번호는 앞 줄이 바뀌면 밀리므로 쓰지 않는다.
줄을 고치면 내용이 달라져 다시 대상이 된다.
"""

from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace

from app.models.finding import Finding

RejectionKey = tuple[str, str, str]  # (파일, 규칙 코드, 앞뒤 공백을 뺀 줄 내용)


def key_of(file: str, code: str, line_text: str) -> RejectionKey:
    return (file, code, line_text.strip())


def finding_key(f: Finding) -> RejectionKey | None:
    """줄 내용을 모르거나 빈 줄이면 None (빈 줄끼리 같다고 보지 않는다)."""
    if f.line_text is None or not f.line_text.strip():
        return None
    return key_of(f.file, f.code, f.line_text)


def with_line_text(
    findings: Iterable[Finding], lines_of: Callable[[str], Sequence[str] | None]
) -> tuple[Finding, ...]:
    """위반마다 그 줄의 내용을 채운다. lines_of는 파일의 줄 목록(읽지 못하면 None)."""
    cache: dict[str, Sequence[str] | None] = {}
    out = []
    for f in findings:
        if f.file not in cache:
            cache[f.file] = lines_of(f.file)
        lines = cache[f.file]
        text = lines[f.line - 1] if lines is not None and 0 < f.line <= len(lines) else None
        out.append(replace(f, line_text=text.strip() if text is not None else None))
    return tuple(out)


def mark_rejected(
    findings: Iterable[Finding], rejected: Mapping[RejectionKey, int]
) -> tuple[Finding, ...]:
    """거절된 제안이 고친 것과 같은 위반에 그 제안 PR 번호를 적는다."""
    out = []
    for f in findings:
        key = finding_key(f)
        number = rejected.get(key) if key is not None else None
        out.append(replace(f, rejected_in=number) if number is not None else f)
    return tuple(out)


def fixed_keys(before: Iterable[Finding], remaining: Iterable[Finding]) -> list[RejectionKey]:
    """수정안이 고친 위반의 기록 키. 고친 뒤에도 남은 위반(파일·규칙·메시지로 센다)은 빼고,
    이미 거절된 위반은 수정 대상이 아니었으므로 넣지 않는다."""
    left = Counter((f.file, f.code, f.message) for f in remaining)
    keys = []
    for f in before:
        ident = (f.file, f.code, f.message)
        if left[ident] > 0:
            left[ident] -= 1
            continue
        key = finding_key(f)
        if key is not None and f.rejected_in is None:
            keys.append(key)
    return keys
