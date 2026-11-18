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
from app.sandbox.ruff_runner import FormatResult
from app.worker import repo
from app.worker.base_config import BaseConfig
from app.worker.repo import CHANGES_DIFF

PYTHON_SUFFIXES: Final = (".py", ".pyi")
# 겹치는 수정은 한 회차에 하나만 적용하고 다음 회차에 다시 돌린다 (ruff --fix와 같은 방식)
MAX_FIX_ROUNDS: Final = 5


def changed_python_files(worktree: Path, changed: ChangedLines) -> list[str]:
    return sorted(
        f for f in changed if f.endswith(PYTHON_SUFFIXES) and repo.worktree_file(worktree, f)
    )


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
    names: docker_cli.Names = docker_cli.DEFAULT_NAMES,
    changed: ChangedLines | None = None,
) -> SkillResult | None:
    """.venv 없는 컨테이너에서 ruff를 돌린다. 새 커밋이 와서 중단되면 None.

    changed가 없으면 changes.diff를 읽는다. AI 수정 뒤 새 위반을 볼 때는 다시 센 줄을 넘긴다.

    의존성 설치와 따로 돌리므로, 설치가 실패한 PR에도 규칙 결과를 보여줄 수 있다.
    """
    if changed is None:
        changed = added_lines((worktree / CHANGES_DIFF).read_text())
    files = changed_python_files(worktree, changed)
    if not files:
        return SkillResult(skill="lint", source=base.ruff_source)
    docker_cli.ensure_image(names.image)
    with container.running(worktree, None, job_id=job_id and f"{job_id}-lint", names=names) as name:
        run = ruff_runner.run_ruff(name, files, base.ruff_config, is_stale=is_stale)
    if run.cancelled:
        return None
    return to_result(run, changed, base.ruff_source)


@dataclass(frozen=True)
class LintFix:
    after: SkillResult  # 고친 뒤 PR이 바꾼 줄의 위반
    applied: int  # 적용한 위반 수정 수
    touched_files: tuple[str, ...]  # 수정·포맷을 적용한 파일 (허용 범위)
    formatted_files: tuple[str, ...] = ()  # ruff format으로 바뀐 파일
    format_skipped: str = ""  # 포맷을 하지 않은 이유 (base가 포맷돼 있지 않음 등)


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
        path = repo.worktree_file(worktree, file)
        if path is None:  # changed_python_files가 이미 뺐지만, 쓰기 직전에 다시 본다
            continue
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


BASE_NOT_FORMATTED: Final = "base 브랜치가 ruff format을 따르지 않음"


def base_is_formatted(
    worktree: Path,
    base: BaseConfig,
    base_sha: str,
    *,
    job_id: str | None = None,
    is_stale: Callable[[], bool] | None = None,
    names: docker_cli.Names = docker_cli.DEFAULT_NAMES,
) -> FormatResult | None:
    """base 브랜치 전체가 `ruff format --check`를 통과하는지 (DESIGN.md F7).

    포맷돼 있지 않은 리포에서 PR 파일을 포맷하면 PR이 건드리지 않은 줄까지 diff가 커진다.
    """
    with repo.side_worktree(worktree, base_sha) as base_tree:
        if base.ruff_config is not None:  # base 설정 파일을 같은 경로에 둔다
            target = base_tree / base.ruff_config
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((worktree / base.ruff_config).read_bytes())
        with container.running(
            base_tree, None, job_id=job_id and f"{job_id}-fmt", names=names
        ) as name:
            return ruff_runner.run_format(
                name, ["."], base.ruff_config, check=True, is_stale=is_stale
            )


def _read(worktree: Path, rel: str) -> bytes:
    path = repo.worktree_file(worktree, rel)
    return path.read_bytes() if path is not None else b""


def fix_lint(
    worktree: Path,
    base: BaseConfig,
    merge_base: str,
    *,
    base_sha: str | None = None,
    job_id: str | None = None,
    is_stale: Callable[[], bool] | None = None,
    max_rounds: int = MAX_FIX_ROUNDS,
    names: docker_cli.Names = docker_cli.DEFAULT_NAMES,
) -> LintFix | None:
    """ruff가 낸 안전한 수정 중 PR이 바꾼 줄의 것만 적용하고, 조건이 맞으면 포맷한 뒤 다시 검사한다.

    `ruff check --fix`를 쓰지 않는다. 파일 전체를 고쳐 PR이 건드리지 않은 줄까지 바뀐다.
    회차마다 갈라진 커밋(merge_base)과의 diff로 바뀐 줄을 다시 센다 (수정으로 줄 번호가 밀린다).
    `ruff format`은 base_sha(base 브랜치)가 포맷돼 있을 때만 PR이 바꾼 파일에 적용한다.
    base_sha가 None이면 포맷하지 않는다. 새 커밋이 오면 None.
    """
    format_skipped = ""
    if base_sha is None:
        format_skipped = "포맷 확인 안 함"
    else:
        formatted_base = base_is_formatted(
            worktree, base, base_sha, job_id=job_id, is_stale=is_stale, names=names
        )
        if formatted_base is None:
            return None
        if formatted_base is not True:
            format_skipped = BASE_NOT_FORMATTED if formatted_base is False else formatted_base
    applied, touched = 0, set()
    formatted: list[str] = []
    with container.running(worktree, None, job_id=job_id and f"{job_id}-fix", names=names) as name:
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
        if not format_skipped and run.error is None and files:
            before = {f: _read(worktree, f) for f in files}
            done = ruff_runner.run_format(
                name, files, base.ruff_config, check=False, is_stale=is_stale
            )
            if done is None:
                return None
            if done is True:
                formatted = [f for f in files if _read(worktree, f) != before[f]]
                if formatted:  # 포맷 뒤 줄이 바뀌었으니 다시 검사한다
                    changed = added_lines(repo.working_diff(worktree, merge_base))
                    run = ruff_runner.run_ruff(
                        name, changed_python_files(worktree, changed), base.ruff_config,
                        is_stale=is_stale,
                    )  # fmt: skip
                    if run.cancelled:
                        return None
                    result = to_result(run, changed, base.ruff_source)
            else:
                format_skipped = done
    return LintFix(
        after=result,
        applied=applied,
        touched_files=tuple(sorted(touched | set(formatted))),
        formatted_files=tuple(formatted),
        format_skipped=format_skipped,
    )
