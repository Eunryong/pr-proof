"""lint 스킬: PR이 바꾼 줄의 ruff 위반 (docs/DESIGN.md F6).

ruff는 바뀐 파일 전체를 검사하고, 위반 범위가 바뀐 줄에 걸린 것만 남긴다.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Final

from app.models.diff import ChangedLines, added_lines, touches
from app.models.finding import Finding, SkillResult
from app.sandbox import container, docker_cli, ruff_runner
from app.worker.base_config import BaseConfig
from app.worker.repo import CHANGES_DIFF

PYTHON_SUFFIXES: Final = (".py", ".pyi")


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
