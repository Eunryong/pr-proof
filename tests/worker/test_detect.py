"""worker/detect.py: 채택한 검사기를 PR이 바꾼 파일에 돌려 바뀐 줄의 위반만 남긴다."""

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest

from app.models.checker import Checker, CheckerKind
from app.models.checker_authoring import Checked, Unchecked
from app.models.diff import added_lines
from app.models.rule import CheckMethod, Rule, RuleSource
from app.models.violation import violation_id
from app.sandbox.checker_runner import CheckerRun, Failed, Match, Ran
from app.worker.detect import detect

RULE: Final = Rule(
    id="R-readme-1", text="`orders/` 아래 모듈은 `print`를 쓰지 않는다.",
    source=RuleSource("README.md", "코드 규칙 > 출력", "abc"), origin="document",
    scope=("orders/**",), check=CheckMethod("checker"), fix="model",
)  # fmt: skip
CHECKER: Final = Checker(
    id="c1", rule_id=RULE.id, rule_text_hash="h", kind="ast-grep", body="grep:print(",
    examples=(), engine_version="ast-grep-0.51.3", verified_at=datetime(2026, 11, 18, tzinfo=UTC),
)  # fmt: skip
CHECKED: Final = Checked(RULE, CHECKER, cached=True)


class GrepSandbox:
    """본문 `grep:<글>`이 든 줄을 잡는다. 받은 파일 목록을 남긴다."""

    def __init__(self, result: CheckerRun | None = None) -> None:
        self.result = result
        self.files: list[list[str]] = []

    def on_examples(self, kind: CheckerKind, body: str, snippets: Sequence[str]) -> CheckerRun:
        raise AssertionError("위반 찾기는 예시를 돌리지 않는다")

    def on_files(
        self, kind: CheckerKind, body: str, root: Path, files: Sequence[str]
    ) -> CheckerRun:
        self.files.append(list(files))
        if self.result is not None:
            return self.result
        needle = body.removeprefix("grep:")
        return Ran(
            tuple(
                Match(f, n, n, "print를 쓰지 않는다")
                for f in files
                for n, line in enumerate((root / f).read_text().split("\n"), 1)
                if needle in line
            )
        )


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    (tmp_path / "orders" / "services").mkdir(parents=True)
    (tmp_path / "orders" / "services" / "pricing.py").write_text(
        "def total(o):\n    print('old')\n    print('new')\n    return 1\n"
    )
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "run.py").write_text("print('outside scope')\n")
    (tmp_path / "orders" / "README.md").write_text("print(\n")
    return tmp_path


def _diff(path: str, start: int, count: int) -> str:
    body = "".join("+line\n" for _ in range(count))
    head = f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
    return f"{head}@@ -0,0 +{start},{count} @@\n{body}"


CHANGED: Final = added_lines(
    _diff("orders/services/pricing.py", 3, 1)
    + _diff("scripts/run.py", 1, 1)
    + _diff("orders/README.md", 1, 1)
    + _diff("orders/gone.py", 1, 1)
)


def test_keeps_only_violations_on_changed_lines(worktree: Path) -> None:
    sandbox = GrepSandbox()
    result = detect([CHECKED], worktree, CHANGED, sandbox=sandbox)
    assert sandbox.files == [["orders/services/pricing.py"]]  # 범위 밖, .py 아님, 없는 파일 제외
    (violation,) = result.violations
    assert (violation.file, violation.line, violation.line_text) == (
        "orders/services/pricing.py", 3, "print('new')"
    )  # fmt: skip
    assert violation.rule_id == RULE.id and violation.found_by == "checker"
    assert violation.message == "print를 쓰지 않는다"
    assert result.unchecked == ()


def test_violation_id_survives_lines_added_above(worktree: Path) -> None:
    (before,) = detect([CHECKED], worktree, CHANGED, sandbox=GrepSandbox()).violations
    path = worktree / "orders" / "services" / "pricing.py"
    path.write_text("import logging\n\n" + path.read_text())
    moved = added_lines(_diff("orders/services/pricing.py", 5, 1))
    (after,) = detect([CHECKED], worktree, moved, sandbox=GrepSandbox()).violations
    assert after.line == 5 and after.id == before.id


def test_identical_lines_get_distinct_ids(worktree: Path) -> None:
    path = worktree / "orders" / "services" / "pricing.py"
    path.write_text("print('x')\nprint('x')\n")
    changed = added_lines(_diff("orders/services/pricing.py", 1, 2))
    first, second = detect([CHECKED], worktree, changed, sandbox=GrepSandbox()).violations
    assert first.id == violation_id(RULE.id, first.file, "print('x')", 0)
    assert second.id == violation_id(RULE.id, first.file, "print('x')", 1)


def test_rules_without_checker_are_not_run(worktree: Path) -> None:
    sandbox = GrepSandbox()
    unchecked = Unchecked(RULE, "no_checker", "3회 실패")
    assert detect([unchecked], worktree, CHANGED, sandbox=sandbox).violations == ()
    assert sandbox.files == []


def test_scope_limits_files(worktree: Path) -> None:
    sandbox = GrepSandbox()
    models_only = replace(CHECKED, rule=replace(RULE, scope=("orders/models/**",)))
    assert detect([models_only], worktree, CHANGED, sandbox=sandbox).violations == ()
    assert sandbox.files == []
    everywhere = replace(CHECKED, rule=replace(RULE, scope=()))
    found = detect([everywhere], worktree, CHANGED, sandbox=sandbox).violations
    assert {v.file for v in found} == {"orders/services/pricing.py", "scripts/run.py"}


def test_checker_failing_on_pr_files_marks_rule_unchecked(worktree: Path) -> None:
    result = detect([CHECKED], worktree, CHANGED, sandbox=GrepSandbox(Failed("120초 안에")))
    assert result.violations == ()
    (unchecked,) = result.unchecked
    assert unchecked.kind == "run_failed" and "120초" in unchecked.detail


def test_match_beyond_file_end_is_dropped(worktree: Path) -> None:
    sandbox = GrepSandbox(Ran((Match("orders/services/pricing.py", 99, 99, "m"),)))
    changed = added_lines(_diff("orders/services/pricing.py", 99, 1))
    assert detect([CHECKED], worktree, changed, sandbox=sandbox).violations == ()
