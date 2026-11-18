"""worker/skills/lint.py: 바뀐 줄의 위반만 남긴다."""

from pathlib import Path
from typing import Final

import pytest

from app.models.repo_config import RepoConfig
from app.sandbox.ruff_runner import RuffRun, RuffViolation
from app.worker.base_config import BaseConfig
from app.worker.repo import CHANGES_DIFF
from app.worker.skills import lint
from tests.sandbox_names import TEST_NAMES


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
    result = lint.run_lint(worktree, base, names=TEST_NAMES)
    assert result is not None and result.checked
    assert [(f.file, f.line, f.code) for f in result.findings] == [
        ("orders/lint_case.py", 2, "F401")
    ]


def test_apply_safe_fixes_only_on_changed_lines(tmp_path: Path) -> None:
    from app.models.text_edit import TextEdit

    (tmp_path / "a.py").write_text("import os\nimport sys\n")

    def delete_line(row: int) -> tuple[TextEdit, ...]:
        return (TextEdit(row, 1, row + 1, 1, ""),)

    run = RuffRun(
        violations=(
            RuffViolation("a.py", 1, 1, "F401", "os", delete_line(1)),  # 기존 줄
            RuffViolation("a.py", 2, 2, "F401", "sys", delete_line(2)),  # PR이 바꾼 줄
        )
    )
    applied, touched = lint.apply_safe_fixes(tmp_path, run, {"a.py": frozenset({2})})
    assert (applied, touched) == (1, {"a.py"})
    assert (tmp_path / "a.py").read_text() == "import os\n"  # PR이 안 건드린 줄은 그대로


def _git(cwd: Path, *args: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip


@pytest.mark.docker
def test_real_fix_touches_only_changed_lines(tmp_path: Path) -> None:
    """base에 이미 있던 위반은 그대로 두고, PR이 추가한 줄의 위반만 고친다.

    한글이 든 줄에서 열 위치(문자 단위)가 맞는지도 본다.
    """
    wt = tmp_path / "wt"
    wt.mkdir()
    _git(wt, "init", "-q", "-b", "main")
    (wt / "mod.py").write_text('import os\n\nMSG = "기존"\n')
    _git(wt, "add", ".")
    _git(wt, "commit", "-q", "-m", "base")
    base_sha = _git(wt, "rev-parse", "HEAD")
    # PR: 안 쓰는 import(안전한 수정), 한글 뒤의 f-string(같은 줄 중간의 안전한 수정),
    # 안 쓰는 지역 변수(F841, ruff의 수정이 unsafe라 적용하지 않음)
    (wt / "mod.py").write_text(
        'import os\nimport sys\n\nMSG = "기존"\nOK = "한글" + f"끝"\n\n\ndef f():\n    unused = 1\n'
    )
    base = BaseConfig(
        repo=RepoConfig(), memory_gb=1, max_memory_gb=4, ruff_config=None, ruff_source="기본"
    )
    result = lint.fix_lint(wt, base, base_sha, names=TEST_NAMES)
    assert result is not None and result.applied == 2
    text = (wt / "mod.py").read_text()
    assert text.startswith("import os\n")  # base에 있던 위반 (F401 os)은 그대로
    assert "import sys" not in text
    assert 'OK = "한글" + "끝"\n' in text  # 열 위치가 문자 단위로 맞다
    assert result.touched_files == ("mod.py",)
    assert [f.code for f in result.after.findings] == ["F841"]


def _repo_with_pr(tmp_path: Path, base_files: dict[str, str], pr_files: dict[str, str]):
    wt = tmp_path / "wt"
    wt.mkdir()
    _git(wt, "init", "-q", "-b", "main")
    for name, text in base_files.items():
        (wt / name).write_text(text)
    _git(wt, "add", ".")
    _git(wt, "commit", "-q", "-m", "base")
    base_sha = _git(wt, "rev-parse", "HEAD")
    for name, text in pr_files.items():
        (wt / name).write_text(text)
    return wt, base_sha


DEFAULT_BASE: Final = BaseConfig(
    repo=RepoConfig(), memory_gb=1, max_memory_gb=4, ruff_config=None, ruff_source="기본"
)


@pytest.mark.docker
def test_format_applies_when_base_is_formatted(tmp_path: Path) -> None:
    wt, base_sha = _repo_with_pr(
        tmp_path, {"mod.py": "X = 1\n"}, {"mod.py": "X = 1\nY = {'a':1}\n"}
    )
    result = lint.fix_lint(wt, DEFAULT_BASE, base_sha, base_sha=base_sha, names=TEST_NAMES)
    assert result is not None and result.formatted_files == ("mod.py",)
    assert (wt / "mod.py").read_text() == 'X = 1\nY = {"a": 1}\n'
    assert result.touched_files == ("mod.py",)


@pytest.mark.docker
def test_format_is_skipped_when_base_is_not_formatted(tmp_path: Path) -> None:
    """base에 포맷되지 않은 파일이 있으면 PR 파일도 포맷하지 않는다 (diff가 커지는 것을 막음)."""
    wt, base_sha = _repo_with_pr(
        tmp_path,
        {"mod.py": "X = 1\n", "old.py": "Z = {'b':2}\n"},
        {"mod.py": "X = 1\nY = {'a':1}\n"},
    )
    result = lint.fix_lint(wt, DEFAULT_BASE, base_sha, base_sha=base_sha, names=TEST_NAMES)
    assert result is not None and result.formatted_files == ()
    assert result.format_skipped == lint.BASE_NOT_FORMATTED
    assert "{'a':1}" in (wt / "mod.py").read_text()
    assert not (tmp_path / f"wt-{base_sha[:8]}").exists()  # base worktree는 지운다
