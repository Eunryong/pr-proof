"""worker/skills/lint.py: 바뀐 줄의 위반만 남긴다."""

from pathlib import Path
from typing import Final

import pytest

from app.models.repo_config import RepoConfig
from app.sandbox.ruff_runner import RuffRun, RuffViolation
from app.worker.base_config import BaseConfig
from app.worker.repo import CHANGES_DIFF
from app.worker.skills import lint


def _v(file: str, line: int, end: int | None = None) -> RuffViolation:
    return RuffViolation(file=file, line=line, end_line=end or line, code="F401", message="m")


CHANGED: Final = {"a.py": frozenset({3, 4})}


def test_only_violations_on_changed_lines() -> None:
    run = RuffRun(violations=(_v("a.py", 3), _v("a.py", 10), _v("b.py", 3), _v("a.py", 1, 4)))
    result = lint.to_result(run, CHANGED, "pyproject.toml")
    assert [(f.file, f.line) for f in result.findings] == [("a.py", 3), ("a.py", 1)]
    assert all(f.skill == "lint" and f.source == "pyproject.toml" for f in result.findings)


def test_ruff_error_becomes_unchecked() -> None:
    result = lint.to_result(RuffRun(error="boom"), CHANGED, "x")
    assert not result.checked and result.findings == ()


def test_changed_python_files(tmp_path: Path) -> None:
    for name in ("a.py", "b.pyi", "c.txt"):
        (tmp_path / name).write_text("")
    changed = {n: frozenset({1}) for n in ("a.py", "b.pyi", "c.txt", "gone.py")}
    assert lint.changed_python_files(tmp_path, changed) == ["a.py", "b.pyi"]


@pytest.mark.docker
def test_real_ruff_in_sandbox_reports_only_changed_lines(worktree: Path) -> None:
    """예제 리포에 위반 두 개를 넣고, 그중 한 줄만 PR이 바꾼 것으로 diff를 만든다."""
    (worktree / "orders" / "lint_case.py").write_text("import os\nimport sys\n\nX = 1\n")
    out = worktree / CHANGES_DIFF
    out.parent.mkdir(exist_ok=True)
    out.write_text(
        "diff --git a/orders/lint_case.py b/orders/lint_case.py\n"
        "--- a/orders/lint_case.py\n+++ b/orders/lint_case.py\n"
        "@@ -1,4 +1,4 @@\n import os\n-import json\n+import sys\n \n X = 1\n"
    )
    base = BaseConfig(
        repo=RepoConfig(), memory_gb=1, max_memory_gb=4, ruff_config=None, ruff_source="기본"
    )
    result = lint.run_lint(worktree, base)
    assert result is not None and result.checked
    assert [(f.file, f.line, f.code) for f in result.findings] == [
        ("orders/lint_case.py", 2, "F401")
    ]
