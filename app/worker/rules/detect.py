"""위반 찾기 (docs/DESIGN.md 4.5 도구와 검사기): 채택한 도구와 검사기를 PR이 바꾼 파일에 돌린다.

도구와 검사기는 적용 범위에 드는 바뀐 Python 파일 전체를 검사하고, 잡힌 범위가 PR이 바꾼 줄에 걸린
것만 위반으로 남긴다. 기존 코드의 위반은 PR 작성자의 책임이 아니다. 위반 id는 줄 번호가 아니라
(규칙, 줄 내용, 그 파일에서 같은 내용의 몇 번째인지)로 정해, 위쪽에 줄이 더해져도 같다.

도구 설정에서 온 규칙 묶음(ruff 설정)의 위반은 ruff의 코드를 규칙 id로 갖는다 (4.3). 문서
규칙이 같은 ruff 코드로 같은 줄을 잡으면 문서 규칙의 위반만 남긴다(출처가 문서로 보인다).
`rules.disabled`에 적은 ruff 코드의 위반은 남기지 않는다.
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from app.worker import repo
from app.worker.checker_authoring_pure import Checked, RuleCheck, Unchecked
from app.worker.core.checker_pure import TOOL_KINDS
from app.worker.core.diff_pure import ChangedLines, touches
from app.worker.core.rule_pure import checked_file, looks_at_paths
from app.worker.core.text_edit_pure import TextEdit
from app.worker.core.violation_pure import Violation, violation_id
from app.worker.rules.checkers import RunsCheckers, Sandbox
from app.worker.sandbox import toolcache
from app.worker.sandbox.checker_runner import Failed, Invalid, Match
from app.worker.sandbox.safe_read import read_regular_file

MAX_SOURCE_BYTES: Final = 5 * 1024 * 1024


@dataclass(frozen=True)
class Detection:
    violations: tuple[Violation, ...]
    # 검사기가 있지만 이 PR에서 돌리지 못한 규칙 (kind "run_failed", "tool_unavailable")
    unchecked: tuple[Unchecked, ...] = ()
    # 위반 id → ruff의 안전한 수정. 기계적 수정이 쓴다 (상태에는 넣지 않는다)
    edits: Mapping[str, tuple[TextEdit, ...]] = field(default_factory=dict)


def detect(
    checks: Sequence[RuleCheck],
    worktree: Path,
    changed: ChangedLines,
    *,
    new_files: frozenset[str] = frozenset(),
    sandbox: RunsCheckers | None = None,
    disabled: Collection[str] = (),
) -> Detection:
    """checks 중 검사기가 있는 규칙(`Checked`)을 돌린다. changed는 merge-base 기준 바뀐 줄이다.

    경로 규칙은 PR이 새로 만든 파일(new_files)의 경로만 보고, 위반은 파일 단위다. disabled는
    `rules.disabled`다 (도구 설정 규칙의 ruff 코드를 끈다).

    docker 자체가 실패하면 SandboxError를 올린다 (환경 오류).
    """
    sandbox = sandbox or Sandbox()
    violations: list[Violation] = []
    unchecked: list[Unchecked] = []
    edits: dict[str, tuple[TextEdit, ...]] = {}
    for check in checks:
        if not isinstance(check, Checked):
            continue
        rule, checker = check.rule, check.checker
        by_path = looks_at_paths(rule)
        files = [
            f for f in sorted(new_files if by_path else changed)
            if checked_file(rule, f) and repo.worktree_file(worktree, f) is not None
        ]  # fmt: skip
        if not files:
            continue
        try:
            run = sandbox.on_files(checker.kind, checker.body, worktree, files)
        except toolcache.ToolUnavailable as e:
            unchecked.append(Unchecked(rule, "tool_unavailable", f"도구를 준비하지 못함: {e}"))
            continue
        if isinstance(run, Invalid | Failed):
            unchecked.append(Unchecked(rule, "run_failed", f"검사기를 돌리지 못함: {run.reason}"))
            continue
        if by_path:
            violations += _file_violations(check, run.matches)
        else:
            violations += _violations(check, run.matches, worktree, changed, edits)
    kept = _without_repeats([v for v in violations if v.rule_id not in disabled])
    return Detection(
        tuple(kept), tuple(unchecked), {v.id: edits[v.id] for v in kept if v.id in edits}
    )


def _without_repeats(violations: Sequence[Violation]) -> list[Violation]:
    """문서 규칙이 같은 ruff 코드로 같은 줄을 잡았으면 도구 설정 규칙의 위반을 뺀다."""
    by_document = {(v.file, v.line, v.code) for v in violations if v.code and v.rule_id != v.code}
    return [v for v in violations if v.rule_id != v.code or (v.file, v.line, v.code)
            not in by_document]  # fmt: skip


def _file_violations(check: Checked, matches: Sequence[Match]) -> list[Violation]:
    """파일 단위 위반. 파일마다 하나다. id는 경로로 정한다 (줄 내용이 없다)."""
    return [
        Violation(
            id=violation_id(check.rule.id, file, "", 0), rule_id=check.rule.id, file=file,
            line=0, message=message or check.rule.text, found_by="checker", unit="file",
        )
        for file, message in sorted({m.file: m.message for m in matches}.items())
    ]  # fmt: skip


def _violations(
    check: Checked,
    matches: Sequence[Match],
    worktree: Path,
    changed: ChangedLines,
    edits: dict[str, tuple[TextEdit, ...]],
) -> list[Violation]:
    """바뀐 줄에 걸린 것만 남긴다. 안전한 수정이 있는 도구 위반은 그 편집을 edits에 넣는다.

    같은 줄 내용의 몇 번째인지는 바뀌지 않은 줄의 것까지 파일 순서로 규칙마다 센다.
    """
    result: list[Violation] = []
    lines_by_file: dict[str, list[str]] = {}
    seen: dict[tuple[str, str, str], int] = {}
    tool = check.checker.kind in TOOL_KINDS
    by_code = check.rule.origin == "tool_config"
    for match in sorted(matches, key=lambda m: (m.file, m.line, m.end_line)):
        if match.file not in lines_by_file:
            lines_by_file[match.file] = source_lines(worktree, match.file)
        lines = lines_by_file[match.file]
        if match.line > len(lines):
            continue  # 검사한 뒤 파일이 바뀌었거나 출력이 틀림. 존재하는 줄만 받는다 (4.5)
        line_text = lines[match.line - 1].strip()
        rule_id = match.code if by_code and match.code else check.rule.id
        occurrence = seen.get((rule_id, match.file, line_text), 0)
        seen[(rule_id, match.file, line_text)] = occurrence + 1
        if not touches(changed, match.file, match.line, match.end_line):
            continue
        violation = Violation(
            id=violation_id(rule_id, match.file, line_text, occurrence), rule_id=rule_id,
            file=match.file, line=match.line, message=match.message or check.rule.text,
            found_by="tool" if tool else "checker", code=match.code, line_text=line_text,
            mechanical_fix=bool(match.edits) or check.checker.kind == "ruff-format",
        )  # fmt: skip
        if match.edits:
            edits[violation.id] = match.edits
        result.append(violation)
    return result


def source_lines(worktree: Path, rel: str) -> list[str]:
    """ast-grep과 같이 `\\n`으로만 줄을 나눈다. 읽을 수 없으면 빈 목록 (그 파일의 위반은 버린다)."""
    path = repo.worktree_file(worktree, rel)
    data = read_regular_file(path, max_bytes=MAX_SOURCE_BYTES) if path is not None else None
    if data is None:
        return []
    return data.decode("utf-8", errors="replace").split("\n")
