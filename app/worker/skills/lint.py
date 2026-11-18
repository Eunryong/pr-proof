"""lint 스킬: PR이 바꾼 줄의 ruff 위반 (docs/DESIGN.md F6).

ruff는 바뀐 파일 전체를 검사하고, 위반 범위가 바뀐 줄에 걸린 것만 남긴다.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.models.diff import ChangedLines, added_lines, touches
from app.models.finding import Finding, SkillResult
from app.models.text_edit import apply_edits
from app.sandbox import container, docker_cli, ruff_runner
from app.worker import repo
from app.worker.base_config import BaseConfig
from app.worker.repo import CHANGES_DIFF

PYTHON_SUFFIXES: Final = (".py", ".pyi")
# 겹치는 수정은 한 회차에 하나만 적용하고 다음 회차에 다시 돌린다 (ruff --fix와 같은 방식)
MAX_FIX_ROUNDS: Final = 5


def changed_python_files(worktree: Path, changed: ChangedLines) -> list[str]:
    return sorted(f for f in changed if f.endswith(PYTHON_SUFFIXES) and (worktree / f).is_file())


def to_result(run: ruff_runner.RuffRun, changed: ChangedLines, source: str) -> SkillResult:
    if run.error is not None:
        return SkillResult(skill="lint", source=source, error=run.error)
    findings = tuple(
        Finding(
            file=v.file,
            line=v.line,
            severity="error",
            message=v.message,
            skill="lint",
            source=source,
            code=v.code,
            fixable=v.fixable,
        )
        for v in run.violations
        if touches(changed, v.file, v.line, v.end_line)
    )
    return SkillResult(skill="lint", findings=findings, source=source)


def run_lint(
    worktree: Path,
    base: BaseConfig,
    *,
    job_id: str | None = None,
    is_stale: Callable[[], bool] | None = None,
) -> SkillResult | None:
    """.venv 없는 컨테이너에서 ruff를 돌린다. 새 커밋이 와서 중단되면 None.

    의존성 설치와 따로 돌리므로, 설치가 실패한 PR에도 규칙 결과를 보여줄 수 있다.
    """
    changed = added_lines((worktree / CHANGES_DIFF).read_text())
    files = changed_python_files(worktree, changed)
    if not files:
        return SkillResult(skill="lint", source=base.ruff_source)
    docker_cli.ensure_image()
    with container.running(worktree, None, job_id=job_id and f"{job_id}-lint") as name:
        run = ruff_runner.run_ruff(name, files, base.ruff_config, is_stale=is_stale)
    if run.cancelled:
        return None
    return to_result(run, changed, base.ruff_source)


@dataclass(frozen=True)
class LintFix:
    after: SkillResult  # 고친 뒤 PR이 바꾼 줄의 위반
    applied: int  # 적용한 위반 수정 수
    touched_files: tuple[str, ...]  # 수정을 적용한 파일 (허용 범위)


def apply_safe_fixes(
    worktree: Path, run: ruff_runner.RuffRun, changed: ChangedLines
) -> tuple[int, set[str]]:
    """PR이 바꾼 줄에 걸린 위반의 안전한 수정만 파일에 적용한다. (적용 수, 바꾼 파일)."""
    by_file: dict[str, list[ruff_runner.RuffViolation]] = {}
    for v in run.violations:
        if v.fixable and touches(changed, v.file, v.line, v.end_line):
            by_file.setdefault(v.file, []).append(v)
    applied, touched = 0, set()
    for file, violations in by_file.items():
        path = worktree / file
        # newline=""로 읽고 써서 CRLF 같은 줄바꿈을 그대로 둔다
        with path.open(encoding="utf-8", newline="") as f:
            text = f.read()
        new_text, count = apply_edits(text, [v.edits for v in violations])
        if count and new_text != text:
            with path.open("w", encoding="utf-8", newline="") as f:
                f.write(new_text)
            applied += count
            touched.add(file)
    return applied, touched


def fix_lint(
    worktree: Path,
    base: BaseConfig,
    merge_base: str,
    *,
    job_id: str | None = None,
    is_stale: Callable[[], bool] | None = None,
    max_rounds: int = MAX_FIX_ROUNDS,
) -> LintFix | None:
    """ruff가 낸 안전한 수정 중 PR이 바꾼 줄의 것만 적용하고, 고친 뒤 다시 검사한다.

    `ruff check --fix`를 쓰지 않는다. 파일 전체를 고쳐 PR이 건드리지 않은 줄까지 바뀐다.
    회차마다 갈라진 커밋(merge_base)과의 diff로 바뀐 줄을 다시 센다 (수정으로 줄 번호가 밀린다).
    새 커밋이 오면 None.
    """
    applied, touched = 0, set()
    with container.running(worktree, None, job_id=job_id and f"{job_id}-fix") as name:
        for round_no in range(max_rounds + 1):
            changed = added_lines(repo.working_diff(worktree, merge_base))
            files = changed_python_files(worktree, changed)
            run = ruff_runner.run_ruff(name, files, base.ruff_config, is_stale=is_stale)
            if run.cancelled:
                return None
            result = to_result(run, changed, base.ruff_source)
            if run.error is not None or round_no >= max_rounds:
                break
            count, files_touched = apply_safe_fixes(worktree, run, changed)
            if count == 0:
                break
            applied += count
            touched |= files_touched
    return LintFix(after=result, applied=applied, touched_files=tuple(sorted(touched)))
