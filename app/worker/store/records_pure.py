"""팀의 과거 기록 (docs/DESIGN.md 4.10, 6.1, 7.6). 기록 만들기와 읽은 기록으로 판단하기만 한다.

기록은 세 가지다: 채택(수정안 PR 머지, 작성자가 자기 커밋으로 고침), 거절(사람이 수정안 PR을
닫음), 실패(수정이 검증에 실패함). 검색과 임베딩은 5단계다(D5). 그 전에 기록을 쓰는 곳은 정확히
같은 줄의 거절을 찾는 것(4.6 표의 첫 줄, 6.4의 SQL)뿐이다.

기록의 글은 저장소에서 온 것이라 믿지 않는다. 저장 전에 비밀값 검사를 하고(`text_of`), 보이거나
모델에 넘길 때는 자료로만 다룬다.
"""

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from typing import Final, Literal

from app.shared.db_pure import Record, SeenViolation
from app.worker.core.diff_pure import hunk_removing
from app.worker.core.violation_pure import Violation

RecordKind = Literal["adopted", "rejected", "failed", "checker"]
RecordOrigin = Literal["fix_pr", "author_commit", "verification", "ruleset"]
MAX_CHECKER_CHARS: Final = 8000  # 검사기 기록에 넣는 본문과 예시의 글자 수
CONTEXT_RADIUS: Final = 5  # 위반 줄 앞뒤 줄 수 (6.1)
MAX_DIFF_LINES: Final = 60  # 기록에 넣는 관련 hunk의 줄 수 (6.1)
MAX_DETAIL_CHARS: Final = 2000

# (파일, 규칙 id, 앞뒤 공백을 뺀 줄 내용) → 그 줄을 고친 수정안 PR 번호
RejectedLines = Mapping[tuple[str, str, str], int]


@dataclass(frozen=True)
class Target:
    """수정 시도가 고친 위반 하나와 그 자료. `fix_attempts.targets_json`에 들어가고, 그 수정안이
    머지·닫히거나 검증에 실패하면 이것으로 기록을 만든다."""

    violation_id: str
    rule_id: str
    rule_text: str
    file: str
    line: int
    line_text: str
    context: str  # 고치기 전 위반 줄과 앞뒤 5줄


@dataclass(frozen=True)
class NewRecord:
    """저장할 기록 하나. id와 시각은 저장할 때 붙는다."""

    repo: str
    kind: RecordKind
    origin: RecordOrigin
    source_id: str
    rule_id: str
    rule_text: str
    file: str
    line_text: str
    context: str
    diff: str
    detail: str = ""
    pr_number: int | None = None
    fix_pr_number: int | None = None
    commit_sha: str | None = None


def context_around(lines: Sequence[str], line: int, radius: int = CONTEXT_RADIUS) -> str:
    """line(1부터)과 앞뒤 radius줄. 없는 줄이면 ""."""
    if not 0 < line <= len(lines):
        return ""
    return "\n".join(lines[max(0, line - 1 - radius) : line + radius])


def targets_to_json(targets: Sequence[Target]) -> str:
    return json.dumps([asdict(t) for t in targets], ensure_ascii=False)


def targets_from_json(text: str) -> tuple[Target, ...]:
    return tuple(Target(**item) for item in json.loads(text))


def _from_target(
    t: Target, kind: RecordKind, origin: RecordOrigin, source_id: str, patch: str, **more
) -> NewRecord:
    return NewRecord(
        kind=kind, origin=origin, source_id=source_id, rule_id=t.rule_id, rule_text=t.rule_text,
        file=t.file, line_text=t.line_text, context=t.context,
        diff=hunk_removing(patch, t.file, t.line_text, MAX_DIFF_LINES), **more,
    )  # fmt: skip


def fix_pr_records(
    kind: Literal["adopted", "rejected"],
    *,
    repo: str,
    pr_number: int,
    fix_pr_number: int,
    commit_sha: str | None,
    targets: Sequence[Target],
    patch: str,
) -> list[NewRecord]:
    """수정안 PR이 머지(adopted)되거나 사람이 닫았을(rejected) 때, 그 수정안이 고친 위반마다
    하나."""
    return [
        _from_target(
            t,
            kind,
            "fix_pr",
            f"fix_pr:{fix_pr_number}:{t.violation_id}",
            patch,
            repo=repo,
            pr_number=pr_number,
            fix_pr_number=fix_pr_number,
            commit_sha=commit_sha,
        )  # fmt: skip
        for t in targets
    ]


_FAILURE_LABELS: Final[Mapping[str, str]] = {
    "failed": "검증 실패",
    "dropped": "수정안 버림",
    "new_violations": "새 위반 발생",
    "out_of_scope": "범위 밖 변경",
    "secret": "비밀값 감지",
    "tests_changed": "테스트 실패",
    "unchecked": "재검사 실패",
    "too_big": "크기 상한 초과",
}


def format_failure_detail(detail: str, status: str = "") -> str:
    """실패 기록의 detail 앞에 실패 종류 표식을 붙여 수정 방법 외의 실패와 구별한다."""
    text = detail.strip()
    if status == "failed" and "남은 위반" in text:
        label = "남은 위반"
    else:
        label = _FAILURE_LABELS.get(status, "")
    if label and not text.startswith(f"[{label}]"):
        text = f"[{label}] {text}" if text else f"[{label}]"
    return text[:MAX_DETAIL_CHARS]


def failure_records(
    *,
    repo: str,
    pr_number: int,
    head_sha: str,
    attempt_id: int,
    targets: Sequence[Target],
    patch: str,
    detail: str,
    status: str = "",
) -> list[NewRecord]:
    """수정이 검증에 실패했을 때 대상 위반마다 하나. patch는 마지막 회차가 남긴 diff다."""
    formatted = format_failure_detail(detail, status)
    return [
        _from_target(
            t,
            "failed",
            "verification",
            f"attempt:{attempt_id}:{t.violation_id}",
            patch,
            repo=repo,
            pr_number=pr_number,
            commit_sha=head_sha,
            detail=formatted,
        )  # fmt: skip
        for t in targets
    ]


def author_fixed(
    previous: Iterable[SeenViolation],
    current_ids: frozenset[str],
    checked_rules: frozenset[str],
    lines_of: Callable[[str], Sequence[str] | None],
) -> list[SeenViolation]:
    """이전 커밋의 위반 중 작성자가 자기 커밋으로 고친 것 (4.10): 이번 검사에 없고, 그 규칙을
    이번에도 검사기로 확인했고, 그 파일이 있고, 그 줄 내용이 파일에서 사라졌다.

    규칙이 바뀌었거나 검사하지 못했으면 위반이 없어져도 고친 것으로 보지 않는다. 파일을 지운 것도
    고친 것으로 보지 않는다. 파일 단위 위반은 줄 내용이 없어 보지 않는다.
    """
    out = []
    for v in previous:
        if v.unit != "line" or not v.line_text or v.violation_id in current_ids:
            continue
        if v.rule_id not in checked_rules:
            continue
        lines = lines_of(v.file)
        if lines is None or v.line_text in {line.strip() for line in lines}:
            continue
        out.append(v)
    return out


def author_records(
    fixed: Iterable[SeenViolation],
    *,
    rule_texts: Mapping[str, str],
    head_sha: str,
    diff_of: Callable[[str], str],
) -> list[NewRecord]:
    """작성자가 고친 위반의 채택 기록. diff_of(파일)은 이전 커밋과 이번 커밋 사이의 그 파일
    diff다. 원천 id는 그 위반을 본 커밋과 위반 id다. 그 뒤의 커밋이 같은 이전 기록과 다시 비교해도
    같은 고침을 두 번 남기지 않는다."""
    return [
        NewRecord(
            repo=v.repo, kind="adopted", origin="author_commit",
            source_id=f"seen:{v.head_sha}:{v.violation_id}", rule_id=v.rule_id,
            rule_text=rule_texts.get(v.rule_id, ""), file=v.file, line_text=v.line_text or "",
            context=v.context, diff=hunk_removing(diff_of(v.file), v.file, v.line_text or "",
                                                  MAX_DIFF_LINES),
            pr_number=v.pr_number, commit_sha=head_sha,
        )
        for v in fixed
    ]  # fmt: skip


def checker_record(
    *, repo: str, rule_id: str, rule_text: str, checker_id: str, kind: str, body: str,
    examples: Sequence[tuple[str, Sequence[int]]],
) -> NewRecord:  # fmt: skip
    """예시 확인을 통과해 채택한 검사기의 기록 (4.10 표의 마지막 줄, 6.1). 검색 키는 규칙 원문이고
    꺼내 쓰는 것은 검사기 본문과 예시다. 원천 id는 규칙과 검사기 본문의 해시라 같은 검사기를 두 번
    남기지 않는다."""
    shown = [f"# 형식: {kind}", body.rstrip()]
    for code, lines in examples:
        label = f"위반 예시, 어긴 줄 {list(lines)}" if lines else "정상 예시"
        shown += ["", f"# {label}", code.rstrip()]
    return NewRecord(
        repo=repo, kind="checker", origin="ruleset", source_id=f"checker:{rule_id}:{checker_id}",
        rule_id=rule_id, rule_text=rule_text, file="", line_text="", context="",
        diff="\n".join(shown)[:MAX_CHECKER_CHARS],
    )  # fmt: skip


def text_of(record: NewRecord) -> str:
    """비밀값 검사(6.5, 4.8의 3번과 같은 검사)를 할 글 전체."""
    return "\n".join(
        (record.rule_text, record.file, record.line_text, record.context, record.diff,
         record.detail)
    )  # fmt: skip


def rejected_lines(records: Iterable[Record]) -> dict[tuple[str, str, str], int]:
    """사람이 닫은 수정안 PR의 거절 기록 → 같은 줄을 판단할 표 (4.6 표의 첫 줄). 같은 줄이 여러 번
    거절됐으면 나중 기록의 PR 번호를 쓴다."""
    out = {}
    for r in sorted(records, key=lambda r: r.id or 0):
        if r.kind == "rejected" and r.origin == "fix_pr" and r.fix_pr_number is not None:
            out[(r.file, r.rule_id, r.line_text.strip())] = r.fix_pr_number
    return out


def mark_rejected(
    violations: Iterable[Violation], rejected: RejectedLines
) -> tuple[Violation, ...]:
    """거절된 수정안이 고친 것과 같은 (파일, 규칙, 줄 내용)의 위반에 그 PR 번호를 적는다. 줄을
    고치면 내용이 달라져 다시 대상이 된다. 줄 내용이 없거나 빈 위반은 표시하지 않는다."""
    out = []
    for v in violations:
        text = (v.line_text or "").strip()
        number = rejected.get((v.file, v.rule_id, text)) if text else None
        out.append(replace(v, rejected_in=number) if number is not None else v)
    return tuple(out)
