"""고친 위반의 테스트 표시와 만든 테스트의 글 (docs/DESIGN.md 4.9). 수정안 PR 본문과 결과 코멘트가
같이 쓴다.

표시는 셋이다: 있던 테스트가 실행함 / 만든 테스트가 실행함 / 있던 테스트도 만든 테스트도 이 줄을
실행하지 않음. 같은 글이 위반마다 되풀이되지 않게 상태와 파일마다 줄 번호를 모아 한 줄로 쓴다.
모델이 쓴 까닭은 코드 스팬 안에만 둔다.
"""

from collections.abc import Sequence
from typing import Final

from app.worker.core.markdown_pure import inline_code
from app.worker.core.violation_pure import Violation
from app.worker.generated_tests_pure import (
    LINE_LABEL,
    REJECTION_LABEL,
    GeneratedTest,
    LineRun,
    group_lines,
    line_runs,
)
from app.worker.sandbox.docker_cli import MEMORY_LIMIT_GB
from app.worker.test_run_pure import PytestRun

MAX_LINES_SHOWN: Final = 30
MAX_WHY_CHARS: Final = 300


def _gb(value: float) -> str:
    return f"{value:g}GB"


def format_test_line(run: PytestRun, memory_gb: float = MEMORY_LIMIT_GB) -> str:
    """'테스트:' 줄의 글자 표시. 기호를 쓰지 않는다."""
    s = run.summary
    match run.status:
        case "passed":
            if run.flaky:
                return f"통과, 불안정한 테스트 {len(run.flaky)}개"
            return f"통과 ({s.total}개)" if s else "통과"
        case "failed":
            if s is None:
                return "실패"
            return f"실패 ({s.total}개 중 {s.failed + s.errors}개)"
        case "no_tests":
            return "실행된 테스트 없음"
        case "timeout":
            return "시간 초과로 중단"
        case "cancelled":
            return "새 커밋이 올라와 중단"
        case "unrunnable":
            if run.memory_exceeded:
                return f"실행 불가 (메모리 한도 {_gb(memory_gb)}를 넘어 종료됨)"
            return f"실행 불가 ({run.unrunnable_reason})"


def _numbers(lines: Sequence[int]) -> str:
    shown = ", ".join(map(str, lines[:MAX_LINES_SHOWN]))
    more = len(lines) - MAX_LINES_SHOWN
    return f"{shown} 외 {more}곳" if more > 0 else shown


def status_lines(runs: Sequence[LineRun], violations: Sequence[Violation]) -> list[str]:
    """상태마다 한 줄: "- 만든 테스트가 실행함 (3건): `a.py` 줄 2, 5 · `b.py` 줄 3"."""
    groups = group_lines(runs, violations)
    out = []
    for status in LINE_LABEL:
        mine = [g for g in groups if g[0] == status]
        if not mine:
            continue
        parts = []
        for _, file, lines, why in mine:
            part = f"{inline_code(file, 200)} 줄 {_numbers(lines)}"
            if why:
                part += f" (모델이 적은 까닭: {inline_code(why, MAX_WHY_CHARS)})"
            parts.append(part)
        count = sum(len(g[2]) for g in mine)
        out.append(f"- {LINE_LABEL[status]} ({count}건): {' · '.join(parts)}")
    return out


def generated_lines(tests: Sequence[GeneratedTest]) -> list[str]:
    """만든 테스트 수와 그 테스트가 실행한 위반 수, 뺀 테스트와 이유."""
    kept = [t for t in tests if t.accepted]
    dropped = [t for t in tests if not t.accepted]
    out = []
    if kept:
        covered = len({v for t in kept for v in t.covers})
        files = ", ".join(inline_code(t.file, 200) for t in kept)
        out.append(f"- 만든 테스트 {len(kept)}개 (대상 위반 {covered}건을 실행함): {files}")
    if dropped:
        why = ", ".join(
            f"{inline_code(t.file, 200)} ({REJECTION_LABEL[t.rejected]})"
            for t in dropped
            if t.rejected is not None
        )
        out.append(f"- 뺀 테스트 {len(dropped)}개: {why}")
    return out


def untested_count(runs: Sequence[LineRun]) -> int:
    return sum(1 for r in runs if r.status in ("none", "unknown"))


def runs_without_new_tests(violations: Sequence[Violation]) -> tuple[LineRun, ...]:
    """만든 테스트가 없는 수정(기계적 수정 등)의 표시. 있던 테스트의 실행 여부만 본다."""
    return line_runs(violations, ())
