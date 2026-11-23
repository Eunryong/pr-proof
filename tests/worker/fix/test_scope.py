"""app/worker/fix/scope.py: 임시 git 리포의 실제 `git status` 출력으로 확인한다 (PR 코드는 실행 안
함)."""

import os
import subprocess
from pathlib import Path

import pytest

from app.worker.fix.scope import check_scope, snapshot_ignored
from app.worker.fix.scope_pure import AI_POLICY, MECHANICAL_POLICY
from app.worker.git import repo


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip


@pytest.fixture
def wt(tmp_path: Path) -> Path:
    """src/app.py, src/other.py, tests/test_app.py, .github/workflows/ci.yml, 무시된 build/."""
    path = tmp_path / "wt"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    repo.record_git_dir(path)  # 파이프라인의 worktree처럼 저장소 경로를 밖에 적는다
    files = {
        ".gitignore": "build/\n*.log\n",
        "src/app.py": "X = 1\n",
        "src/other.py": "Y = 1\n",
        "src/pkg/.gitignore": "local_*.py\n",
        "tests/test_app.py": "def test_x():\n    assert True\n",
        ".github/workflows/ci.yml": "on: push\n",
    }
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "base")
    (path / "build").mkdir()
    (path / "build" / "old.txt").write_text("cache")  # 이미 있던 무시된 파일
    return path


def _check(wt: Path, before: frozenset[str], allowed: set[str] | None = None):
    return check_scope(wt, allowed or {"src/app.py"}, before)


def _labels(result) -> list[tuple[str, str]]:
    return [(v.path, v.reason) for v in result.violations]


def test_snapshot_lists_files_inside_ignored_dirs(wt: Path) -> None:
    (wt / "x.log").write_text("")
    (wt / ".pipeline-out").mkdir()
    (wt / ".pipeline-out" / "report.json").write_text("{}")
    assert snapshot_ignored(wt) == {"build/old.txt", "x.log"}


def test_modify_allowed_file_passes(wt: Path) -> None:
    before = snapshot_ignored(wt)
    (wt / "src/app.py").write_text("X = 2\n")
    assert _check(wt, before).ok


def test_existing_ignored_file_is_not_a_problem(wt: Path) -> None:
    before = snapshot_ignored(wt)
    (wt / "build/old.txt").write_text("changed")  # 내용이 바뀌어도 이미 있던 파일
    (wt / "src/app.py").write_text("X = 2\n")
    assert _check(wt, before).ok


def test_pipeline_outputs_are_excluded(wt: Path) -> None:
    before = snapshot_ignored(wt)
    (wt / ".pipeline-out").mkdir()
    (wt / ".pipeline-out" / "report.json").write_text("{}")
    (wt / ".venv" / "bin").mkdir(parents=True)
    (wt / ".venv" / "bin" / "python").write_text("")
    assert _check(wt, before).ok


def test_new_file_outside_allowed_fails(wt: Path) -> None:
    before = snapshot_ignored(wt)
    (wt / "src/new.py").write_text("")
    assert _labels(_check(wt, before)) == [("src/new.py", "허용 목록 밖")]


def test_new_file_inside_allowed_passes(wt: Path) -> None:
    before = snapshot_ignored(wt)
    (wt / "src/constants.py").write_text("LIMIT = 3\n")
    assert _check(wt, before, {"src/app.py", "src/constants.py"}).ok


def test_deleting_test_file_fails(wt: Path) -> None:
    before = snapshot_ignored(wt)
    (wt / "tests/test_app.py").unlink()
    result = _check(wt, before, {"src/app.py", "tests/test_app.py"})  # 허용 목록에 있어도
    assert [(v.code, v.path, v.reason) for v in result.violations] == [
        ("D", "tests/test_app.py", "테스트 파일 변경")
    ]


def test_deleting_allowed_file_passes(wt: Path) -> None:
    """코드를 모두 옮겨 빈 파일을 지우는 경우. 허용 목록에 있으면 허락한다."""
    before = snapshot_ignored(wt)
    (wt / "src/other.py").unlink()
    assert _check(wt, before, {"src/app.py", "src/other.py"}).ok


def test_github_change_fails_even_if_allowed(wt: Path) -> None:
    before = snapshot_ignored(wt)
    (wt / ".github/workflows/ci.yml").write_text("on: pull_request\n")
    result = _check(wt, before, {".github/workflows/ci.yml"})
    assert _labels(result) == [(".github/workflows/ci.yml", "`.github/` 변경")]


def test_staged_rename_outside_allowed_fails(wt: Path) -> None:
    before = snapshot_ignored(wt)
    (wt / "lib").mkdir()
    _git(wt, "mv", "src/app.py", "lib/app.py")
    result = _check(wt, before)
    assert [(v.code, v.path, v.reason) for v in result.violations] == [
        ("R", "lib/app.py", "허용 목록 밖")
    ]


def test_unstaged_rename_outside_allowed_fails(wt: Path) -> None:
    """AI는 git을 쓰지 않으므로 이름 변경은 삭제(D)와 새 파일(??)로 보인다."""
    before = snapshot_ignored(wt)
    (wt / "src/app.py").rename(wt / "src/renamed.py")
    assert _labels(_check(wt, before)) == [("src/renamed.py", "허용 목록 밖")]


def test_rename_inside_allowed_passes(wt: Path) -> None:
    before = snapshot_ignored(wt)
    _git(wt, "mv", "src/app.py", "src/core.py")
    assert _check(wt, before, {"src/app.py", "src/core.py"}).ok


def test_new_conftest_in_ignored_dir_fails(wt: Path) -> None:
    before = snapshot_ignored(wt)
    (wt / "build/conftest.py").write_text("import os\n")
    (wt / "src/app.py").write_text("X = 2\n")
    assert _labels(_check(wt, before)) == [("build/conftest.py", "새로 생긴 무시된 파일")]


def test_new_ignored_file_under_nested_gitignore_fails(wt: Path) -> None:
    before = snapshot_ignored(wt)
    (wt / "src/pkg/local_sitecustomize.py").write_text("")
    result = _check(wt, before)
    assert _labels(result) == [("src/pkg/local_sitecustomize.py", "새로 생긴 무시된 파일")]


def test_new_gitignore_hiding_new_file_fails(wt: Path) -> None:
    """새 .gitignore로 새 파일을 숨겨도 둘 다 잡힌다."""
    before = snapshot_ignored(wt)
    (wt / "src/.gitignore").write_text("conftest.py\n")
    (wt / "src/conftest.py").write_text("")
    assert _labels(_check(wt, before)) == [
        ("src/.gitignore", "허용 목록 밖"),
        ("src/conftest.py", "새로 생긴 무시된 파일"),
    ]


@pytest.mark.parametrize(
    "name", ["src/a b.py", "src/한글.py", "src/new\nline.py", 'src/"q".py'], ids=repr
)
def test_unusual_paths_are_read_verbatim(wt: Path, name: str) -> None:
    before = snapshot_ignored(wt)
    (wt / name).write_text("")
    assert _check(wt, before, {"src/app.py", name}).ok
    assert _labels(_check(wt, before)) == [(name, "허용 목록 밖")]


def test_new_symlink_fails_even_if_allowed(wt: Path) -> None:
    before = snapshot_ignored(wt)
    os.symlink("/etc/passwd", wt / "src/link.py")
    result = _check(wt, before, {"src/app.py", "src/link.py"})
    assert _labels(result) == [("src/link.py", "심볼릭 링크")]


def test_allowed_file_turned_into_symlink_fails(wt: Path) -> None:
    before = snapshot_ignored(wt)
    (wt / "src/app.py").unlink()
    os.symlink("other.py", wt / "src/app.py")
    result = _check(wt, before)
    assert [(v.code, v.reason) for v in result.violations] == [("T", "파일 종류 변경")]


def test_lint_policy_rejects_new_file_in_allowed(wt: Path) -> None:
    (wt / "src/new.py").write_text("")
    result = check_scope(wt, {"src/new.py"}, snapshot_ignored(wt), policy=MECHANICAL_POLICY)
    assert [v.label for v in result.violations] == ["?? src/new.py"]


def test_ai_policy_is_default(wt: Path) -> None:
    (wt / "tests/test_app.py").write_text("")
    result = check_scope(wt, {"tests/test_app.py"}, snapshot_ignored(wt))
    assert (
        result.violations
        == check_scope(wt, {"tests/test_app.py"}, snapshot_ignored(wt), policy=AI_POLICY).violations
    )
    assert not result.ok
