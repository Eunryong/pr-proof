"""static 스킬: PR이 바꾼 줄의 정적변수 위반 (docs/DESIGN.md F6).

두 가지를 합친다.
- ruff 규칙 PLR2004(비교의 매직 값), PLW0603(`global` 문). 규칙 선택은 파이프라인이 정하고
  (`--select`), per-file-ignores·`# noqa`·규칙 옵션은 base 브랜치의 ruff 설정을 따른다.
- AST 규칙 (`static_rules`): 반복 리터럴, 함수가 바꾸는 모듈 수준 가변 상태.
테스트 파일과 `.pyi`는 검사하지 않는다.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Final

from app.models.diff import ChangedLines, added_lines, touches
from app.models.finding import Finding, SkillResult
from app.sandbox import container, docker_cli, ruff_runner
from app.worker import repo
from app.worker.base_config import RUFF_DEFAULT_SOURCE, BaseConfig
from app.worker.repo import CHANGES_DIFF
from app.worker.skills import static_rules
from app.worker.skills.static_rules import DEFAULT_MIN_REPEATS, StaticViolation

RUFF_RULES: Final = ("PLR2004", "PLW0603")  # 매직 값, global 문
STATIC_SUFFIX: Final = ".py"
DEFAULT_SOURCE: Final = RUFF_DEFAULT_SOURCE  # "기본 규칙"
MIN_REPEATS_FLOOR: Final = 2  # 1이면 모든 리터럴이 위반이 된다


def static_files(worktree: Path, changed: ChangedLines) -> list[str]:
    """검사할 파일: PR이 바꾼 .py 중 테스트 파일이 아닌 것."""
    return sorted(
        f
        for f in changed
        if f.endswith(STATIC_SUFFIX)
        and not static_rules.is_test_file(f)
        and repo.worktree_file(worktree, f)
    )


def _finding(file: str, line: int, code: str, message: str, source: str) -> Finding:
    return Finding(
        file=file,
        line=line,
        severity="warning",
        message=message,
        skill="static",
        source=source,
        code=code,
        fixable=False,  # 정적변수 수정은 AI가 한다 (DESIGN.md F7)
    )


def ruff_findings(run: ruff_runner.RuffRun, changed: ChangedLines) -> tuple[Finding, ...]:
    """PR이 바꾼 줄에 걸린 PLR2004·PLW0603만. 문법 오류 등 다른 코드는 lint가 알린다."""
    return tuple(
        _finding(v.file, v.line, v.code, v.message, DEFAULT_SOURCE)
        for v in run.violations
        if v.code in RUFF_RULES and touches(changed, v.file, v.line, v.end_line)
    )


def ast_findings(
    worktree: Path,
    files: list[str],
    changed: ChangedLines,
    *,
    min_repeats: int = DEFAULT_MIN_REPEATS,
    repeat_source: str = DEFAULT_SOURCE,
) -> tuple[Finding, ...]:
    """반복 리터럴·모듈 수준 가변 상태. 읽거나 파싱하지 못한 파일은 건너뛴다."""
    findings: list[Finding] = []
    for file in files:
        path = repo.worktree_file(
            worktree, file
        )  # 링크를 따라가 밖의 파일 내용을 코멘트에 싣지 않는다
        if path is None:
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        tree = static_rules.parse(source)
        if tree is None:
            continue
        lines = changed.get(file, frozenset())
        violations: list[tuple[StaticViolation, str]] = [
            (v, repeat_source) for v in static_rules.repeated_literals(tree, lines, min_repeats)
        ]
        violations += [(v, DEFAULT_SOURCE) for v in static_rules.mutable_state(tree, lines)]
        findings += [_finding(file, v.line, v.code, v.message, src) for v, src in violations]
    return tuple(findings)


def run_static(
    worktree: Path,
    base: BaseConfig,
    *,
    min_repeats: int = DEFAULT_MIN_REPEATS,
    source: str = DEFAULT_SOURCE,
    job_id: str | None = None,
    is_stale: Callable[[], bool] | None = None,
    names: docker_cli.Names = docker_cli.DEFAULT_NAMES,
    changed: ChangedLines | None = None,
) -> SkillResult | None:
    """PR이 바꾼 줄의 정적변수 위반. 새 커밋이 와서 중단되면 None.

    changed가 없으면 changes.diff(테스트 전에 만든 PR의 diff)를 읽는다. 수정 뒤 다시 검사할 때는
    갈라진 커밋과 worktree의 diff로 센 줄을 넘긴다 (수정으로 줄이 밀린다).

    min_repeats와 source는 `.pipeline.yaml`의 `skills.static.min_repeats`에서 온다
    (없으면 기본값 3과 "기본 규칙"). source는 반복 리터럴 위반과 결과의 출처로 쓴다.
    ruff는 .venv 없는 컨테이너에서 돌린다 (lint와 같은 방식). AST 검사는 호스트에서 파싱만 한다.
    대상 리포의 ruff 설정이 exclude한 파일은 AST 검사에서도 뺀다.
    ruff가 검사하지 못하면 결과 전체를 검사하지 못함(error)으로 낸다.
    """
    if min_repeats < MIN_REPEATS_FLOOR:
        raise ValueError(f"min_repeats는 2 이상이어야 함: {min_repeats}")
    if changed is None:
        changed = added_lines((worktree / CHANGES_DIFF).read_text())
    files = static_files(worktree, changed)
    if not files:
        return SkillResult(skill="static", source=source)
    docker_cli.ensure_image(names.image)
    with container.running(
        worktree, None, job_id=job_id and f"{job_id}-static", names=names
    ) as name:
        included = ruff_runner.run_show_files(name, files, base.ruff_config, is_stale=is_stale)
        if included is None:
            return None
        if isinstance(included, str):
            return SkillResult(skill="static", source=source, error=included)
        run = ruff_runner.run_ruff(
            name, list(included), base.ruff_config, select=RUFF_RULES, is_stale=is_stale
        )
    if run.cancelled:
        return None
    if run.error is not None:
        return SkillResult(skill="static", source=source, error=run.error)
    found = ruff_findings(run, changed) + ast_findings(
        worktree, list(included), changed, min_repeats=min_repeats, repeat_source=source
    )
    return SkillResult(
        skill="static",
        findings=tuple(sorted(found, key=lambda f: (f.file, f.line, f.code))),
        source=source,
    )
