"""worker/skills/static.py: ruff·AST 결과를 합치고 PR이 바꾼 줄만 남긴다."""

from contextlib import contextmanager
from pathlib import Path
from typing import Final

import pytest

from app.models.repo_config import RepoConfig
from app.sandbox import container, docker_cli, ruff_runner
from app.sandbox.ruff_runner import RuffRun, RuffViolation
from app.worker.base_config import BaseConfig
from app.worker.repo import CHANGES_DIFF
from app.worker.skills import static, static_rules
from tests.sandbox_names import TEST_NAMES

BASE: Final = BaseConfig(
    repo=RepoConfig(), memory_gb=1, max_memory_gb=4, ruff_config=None, ruff_source="기본 규칙"
)


def _v(file: str, line: int, code: str, end: int | None = None) -> RuffViolation:
    return RuffViolation(file=file, line=line, end_line=end or line, code=code, message="m")


def _diff(path: str, lines: list[str]) -> str:
    """새 파일로 추가한 diff (모든 줄이 PR이 바꾼 줄)."""
    body = "".join(f"+{line}\n" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n{body}"
    )


def test_ruff_findings_keep_only_static_codes_on_changed_lines() -> None:
    run = RuffRun(
        violations=(
            _v("a.py", 3, "PLR2004"),
            _v("a.py", 9, "PLR2004"),  # 안 바뀐 줄
            _v("a.py", 4, "PLW0603"),
            _v("a.py", 3, "syntax-error"),  # lint가 알린다
            _v("a.py", 3, "F401"),
        )
    )
    found = static.ruff_findings(run, {"a.py": frozenset({3, 4})})
    assert [(f.line, f.code) for f in found] == [(3, "PLR2004"), (4, "PLW0603")]
    assert all(
        f.skill == "static"
        and f.severity == "warning"
        and not f.fixable
        and f.source == "기본 규칙"
        for f in found
    )


def test_static_files_skip_tests_stubs_and_missing(tmp_path: Path) -> None:
    for name in ("a.py", "b.pyi", "test_c.py", "tests/d.py", "e.txt"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("")
    changed = {
        n: frozenset({1}) for n in ("a.py", "b.pyi", "test_c.py", "tests/d.py", "e.txt", "gone.py")
    }
    assert static.static_files(tmp_path, changed) == ["a.py"]


def test_ast_findings_sources_and_skip_unparseable(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text('CACHE = {}\n\ndef f():\n    CACHE["k"] = g("ab", "ab", "ab")\n')
    (tmp_path / "broken.py").write_text("def f(:\n")
    (tmp_path / "latin.py").write_bytes(b"x = '\xff'\n")
    changed = {f: frozenset({1, 2, 3, 4}) for f in ("a.py", "broken.py", "latin.py")}
    found = static.ast_findings(
        tmp_path, ["a.py", "broken.py", "latin.py"], changed, repeat_source=".pipeline.yaml"
    )
    by_code = {f.code: f for f in found}
    assert set(by_code) == {static_rules.REPEAT_CODE, static_rules.MUTABLE_CODE}
    assert by_code[static_rules.REPEAT_CODE].source == ".pipeline.yaml"
    assert by_code[static_rules.MUTABLE_CODE].source == "기본 규칙"
    assert all(f.file == "a.py" and not f.fixable for f in found)


def test_ast_findings_min_repeats_argument(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text('g("ab", "ab")\n')
    changed = {"a.py": frozenset({1})}
    assert static.ast_findings(tmp_path, ["a.py"], changed) == ()
    assert len(static.ast_findings(tmp_path, ["a.py"], changed, min_repeats=2)) == 1


@pytest.fixture
def fake_sandbox(monkeypatch):
    """docker 없이 run_static을 돌린다. ruff 결과와 검사 대상 파일을 정해 둔다."""
    calls: dict[str, list] = {"select": [], "files": []}

    def install(run: RuffRun, included: ruff_runner.ShowFilesResult | None = None):
        @contextmanager
        def running(worktree, venv, **kwargs):
            yield "c"

        def show_files(name, files, config, **kwargs):
            return tuple(files) if included is None else included

        def run_ruff(name, files, config, *, select=(), **kwargs):
            calls["select"].append(select)
            calls["files"].append(files)
            return run

        monkeypatch.setattr(docker_cli, "ensure_image", lambda image: None)
        monkeypatch.setattr(container, "running", running)
        monkeypatch.setattr(ruff_runner, "run_show_files", show_files)
        monkeypatch.setattr(ruff_runner, "run_ruff", run_ruff)
        return calls

    return install


def _write_pr(tmp_path: Path, files: dict[str, list[str]]) -> None:
    (tmp_path / CHANGES_DIFF).parent.mkdir(parents=True, exist_ok=True)
    diff = ""
    for path, lines in files.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text("".join(f"{line}\n" for line in lines))
        diff += _diff(path, lines)
    (tmp_path / CHANGES_DIFF).write_text(diff)


def test_run_static_combines_ruff_and_ast(tmp_path: Path, fake_sandbox) -> None:
    _write_pr(
        tmp_path, {"a.py": ['g("ab", "ab", "ab")'], "tests/test_a.py": ['g("ab", "ab", "ab")']}
    )
    calls = fake_sandbox(RuffRun(violations=(_v("a.py", 1, "PLR2004"),)))
    result = static.run_static(tmp_path, BASE, source=".pipeline.yaml", names=TEST_NAMES)
    assert result is not None and result.checked and result.source == ".pipeline.yaml"
    assert [(f.file, f.code) for f in result.findings] == [
        ("a.py", "PLR2004"),
        ("a.py", static_rules.REPEAT_CODE),
    ]
    assert calls["select"] == [static.RUFF_RULES]  # 대상 리포의 규칙 선택과 따로
    assert calls["files"] == [["a.py"]]  # 테스트 파일은 넘기지 않는다


def test_run_static_skips_files_excluded_by_repo_ruff(tmp_path: Path, fake_sandbox) -> None:
    _write_pr(tmp_path, {"gen/a.py": ['g("ab", "ab", "ab")'], "b.py": ["x = 2"]})
    calls = fake_sandbox(RuffRun(), included=("b.py",))
    result = static.run_static(tmp_path, BASE, names=TEST_NAMES)
    assert result is not None and result.findings == ()
    assert calls["files"] == [["b.py"]]


def test_run_static_no_python_files_skips_container(tmp_path: Path, monkeypatch) -> None:
    _write_pr(tmp_path, {"README.md": ["x"], "tests/test_a.py": ["x = 1"]})
    monkeypatch.setattr(docker_cli, "ensure_image", lambda image: pytest.fail("no docker"))
    result = static.run_static(tmp_path, BASE, names=TEST_NAMES)
    assert result is not None and result.checked and result.findings == ()


def test_run_static_ruff_error_is_unchecked(tmp_path: Path, fake_sandbox) -> None:
    _write_pr(tmp_path, {"a.py": ['g("ab", "ab", "ab")']})
    fake_sandbox(RuffRun(error="ruff 실행 오류: bad config"))
    result = static.run_static(tmp_path, BASE, names=TEST_NAMES)
    assert result is not None and not result.checked and result.findings == ()


def test_run_static_show_files_error_is_unchecked(tmp_path: Path, fake_sandbox) -> None:
    _write_pr(tmp_path, {"a.py": ["x = 2"]})
    fake_sandbox(RuffRun(), included="ruff 실행 오류: bad config")
    result = static.run_static(tmp_path, BASE, names=TEST_NAMES)
    assert result is not None and result.error == "ruff 실행 오류: bad config"


def test_run_static_cancelled(tmp_path: Path, fake_sandbox, monkeypatch) -> None:
    _write_pr(tmp_path, {"a.py": ["x = 2"]})
    fake_sandbox(RuffRun(cancelled=True))
    assert static.run_static(tmp_path, BASE, names=TEST_NAMES) is None
    monkeypatch.setattr(ruff_runner, "run_show_files", lambda *a, **k: None)
    assert static.run_static(tmp_path, BASE, names=TEST_NAMES) is None


@pytest.mark.parametrize("min_repeats", [1, 0, -3])
def test_run_static_rejects_too_small_min_repeats(tmp_path: Path, min_repeats: int) -> None:
    with pytest.raises(ValueError):
        static.run_static(tmp_path, BASE, min_repeats=min_repeats)


@pytest.mark.docker
def test_real_static_in_sandbox(tmp_path: Path) -> None:
    """실제 ruff로 PLR2004·PLW0603을 찾고, base 설정의 per-file-ignores와 ignore를 확인한다.

    base 설정의 `ignore`는 파이프라인의 `--select`가 대신하므로 PLR2004가 그대로 나오고,
    per-file-ignores에 걸린 파일에서는 나오지 않는다. exclude한 파일은 AST 검사도 건너뛴다.
    """
    _write_pr(
        tmp_path,
        {
            "app/mod.py": [
                "COUNT = 0",
                "",
                "",
                "def f(a):",
                "    global COUNT",
                "    COUNT = 5",
                "    if a > 42:",
                '        return "ab"',
                '    return "ab" + "ab"',
            ],
            "legacy/old.py": ["def g(a):", "    return a > 42"],
            "gen/out.py": ['x = ["ab", "ab", "ab"]'],
        },
    )
    config = tmp_path / ".pipeline-out" / "base" / "ruff.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        'extend-exclude = ["gen"]\n[lint]\nselect = ["E"]\nignore = ["PLR2004"]\n'
        '[lint.per-file-ignores]\n"legacy/*" = ["PLR2004"]\n'
    )
    base = BaseConfig(
        repo=RepoConfig(), memory_gb=1, max_memory_gb=4,
        ruff_config=".pipeline-out/base/ruff.toml", ruff_source="ruff.toml",
    )  # fmt: skip
    result = static.run_static(tmp_path, base, names=TEST_NAMES)
    assert result is not None and result.checked, result
    assert [(f.file, f.line, f.code) for f in result.findings] == [
        ("app/mod.py", 5, "PLW0603"),
        ("app/mod.py", 7, "PLR2004"),
        ("app/mod.py", 8, static_rules.REPEAT_CODE),
    ]


def test_symlinked_files_are_not_read(tmp_path: Path) -> None:
    """PR의 링크를 따라가 worktree 밖 파일의 리터럴을 코멘트에 싣지 않는다."""
    wt, outside = tmp_path / "wt", tmp_path / "outside"
    wt.mkdir()
    outside.mkdir()
    secret = outside / "secret.py"
    secret.write_text('A = "s3cr3t"\nB = "s3cr3t"\nC = "s3cr3t"\n')
    (wt / "link.py").symlink_to(secret)
    (wt / "pkg").symlink_to(outside, target_is_directory=True)
    changed = {"link.py": frozenset({1, 2, 3}), "pkg/secret.py": frozenset({1, 2, 3})}
    assert static.static_files(wt, changed) == []
    findings = static.ast_findings(wt, ["link.py", "pkg/secret.py"], changed)
    assert findings == ()
