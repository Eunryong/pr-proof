"""위반 찾기 (docs/DESIGN.md 4.5 도구와 검사기): 채택한 검사기를 PR이 바꾼 파일에 돌린다.

검사기는 적용 범위에 드는 바뀐 Python 파일 전체를 검사하고, 잡힌 범위가 PR이 바꾼 줄에 걸린 것만
위반으로 남긴다. 기존 코드의 위반은 PR 작성자의 책임이 아니다. 위반 id는 줄 번호가 아니라 줄
내용과 그 파일에서 같은 내용의 몇 번째인지로 정해, 위쪽에 줄이 더해져도 같다.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.models.checker_authoring import Checked, RuleCheck, Unchecked
from app.models.diff import ChangedLines, touches
from app.models.rule import in_scope
from app.models.violation import Violation, violation_id
from app.sandbox import toolcache
from app.sandbox.checker_runner import Failed, Invalid, Match
from app.sandbox.safe_read import read_regular_file
from app.worker import repo
from app.worker.checkers import RunsCheckers, Sandbox

PYTHON_SUFFIXES: Final = (".py", ".pyi")
MAX_SOURCE_BYTES: Final = 5 * 1024 * 1024


@dataclass(frozen=True)
class Detection:
    violations: tuple[Violation, ...]
    # 검사기가 있지만 이 PR에서 돌리지 못한 규칙 (kind "run_failed", "tool_unavailable")
    unchecked: tuple[Unchecked, ...] = ()


def detect(
    checks: Sequence[RuleCheck],
    worktree: Path,
    changed: ChangedLines,
    *,
    sandbox: RunsCheckers | None = None,
) -> Detection:
    """checks 중 검사기가 있는 규칙(`Checked`)을 돌린다. changed는 merge-base 기준 바뀐 줄이다.

    docker 자체가 실패하면 SandboxError를 올린다 (환경 오류).
    """
    sandbox = sandbox or Sandbox()
    violations: list[Violation] = []
    unchecked: list[Unchecked] = []
    for check in checks:
        if not isinstance(check, Checked):
            continue
        rule, checker = check.rule, check.checker
        files = [
            f for f in sorted(changed)
            if f.endswith(PYTHON_SUFFIXES) and in_scope(rule.scope, f)
            and repo.worktree_file(worktree, f) is not None
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
        violations += _violations(check, run.matches, worktree, changed)
    return Detection(tuple(violations), tuple(unchecked))


def _violations(
    check: Checked, matches: Sequence[Match], worktree: Path, changed: ChangedLines
) -> list[Violation]:
    """바뀐 줄에 걸린 것만 남긴다.

    같은 줄 내용의 몇 번째인지는 바뀌지 않은 줄의 것까지 파일 순서로 센다.
    """
    result: list[Violation] = []
    lines_by_file: dict[str, list[str]] = {}
    seen: dict[tuple[str, str], int] = {}
    for match in sorted(matches, key=lambda m: (m.file, m.line, m.end_line)):
        if match.file not in lines_by_file:
            lines_by_file[match.file] = _lines(worktree, match.file)
        lines = lines_by_file[match.file]
        if match.line > len(lines):
            continue  # 검사한 뒤 파일이 바뀌었거나 출력이 틀림. 존재하는 줄만 받는다 (4.5)
        line_text = lines[match.line - 1].strip()
        occurrence = seen.get((match.file, line_text), 0)
        seen[(match.file, line_text)] = occurrence + 1
        if not touches(changed, match.file, match.line, match.end_line):
            continue
        result.append(
            Violation(
                id=violation_id(check.rule.id, match.file, line_text, occurrence),
                rule_id=check.rule.id, file=match.file, line=match.line,
                message=match.message or check.rule.text, found_by="checker",
                line_text=line_text,
            )
        )  # fmt: skip
    return result


def _lines(worktree: Path, rel: str) -> list[str]:
    """ast-grep과 같이 `\\n`으로만 줄을 나눈다. 읽을 수 없으면 빈 목록 (그 파일의 위반은 버린다)."""
    path = repo.worktree_file(worktree, rel)
    data = read_regular_file(path, max_bytes=MAX_SOURCE_BYTES) if path is not None else None
    if data is None:
        return []
    return data.decode("utf-8", errors="replace").split("\n")
