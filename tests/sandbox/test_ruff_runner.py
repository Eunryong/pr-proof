"""app/sandbox/ruff_runner.py 단위 테스트. docker 없이 run_in_sandbox 결과를 정해 둔다."""

import json

import pytest

from app.sandbox import ruff_runner
from app.sandbox.container import ExecResult
from app.sandbox.docker_cli import SandboxError


def _item(file: str, row: int, end_row: int | None = None, code: str | None = "F401") -> dict:
    return {
        "filename": f"/work/{file}",
        "location": {"row": row, "column": 1},
        "end_location": {"row": end_row or row, "column": 5},
        "code": code,
        "message": "msg",
    }


@pytest.fixture
def fake_exec(monkeypatch):
    calls: list[list[str]] = []

    def install(result: ExecResult) -> list[list[str]]:
        def run(container, argv, **kwargs):
            calls.append(argv)
            return result

        monkeypatch.setattr(ruff_runner, "run_in_sandbox", run)
        return calls

    return install


def test_argv_uses_base_config_or_isolated() -> None:
    with_config = ruff_runner.ruff_argv(["a.py"], ".pipeline-out/base/pyproject.toml")
    assert with_config[with_config.index("--config") + 1] == ".pipeline-out/base/pyproject.toml"
    assert "--isolated" not in with_config
    # 설정이 없으면 PR head의 설정을 읽지 않게 격리한다
    assert "--isolated" in ruff_runner.ruff_argv(["a.py"], None)
    for argv in (with_config, ruff_runner.ruff_argv(["a.py"], None)):
        assert "--no-cache" in argv  # worktree에 .ruff_cache를 남기지 않는다
        assert "--force-exclude" in argv
        assert argv[-2:] == ["--", "a.py"]


def test_parse_output_relative_paths_and_ranges() -> None:
    out = json.dumps([_item("orders/a.py", 3, 5), _item("b.py", 1, code=None)])
    v1, v2 = ruff_runner.parse_output(out)
    assert (v1.file, v1.line, v1.end_line, v1.code) == ("orders/a.py", 3, 5, "F401")
    assert v2.code == ruff_runner.SYNTAX_ERROR_CODE


def test_no_files_skips_container(fake_exec) -> None:
    calls = fake_exec(ExecResult(0, "[]", ""))
    assert ruff_runner.run_ruff("c", [], None) == ruff_runner.RuffRun()
    assert calls == []


@pytest.mark.parametrize("code", [0, 1])
def test_ok_exit_codes(fake_exec, code: int) -> None:
    fake_exec(ExecResult(code, json.dumps([_item("a.py", 1)] if code else []), ""))
    run = ruff_runner.run_ruff("c", ["a.py"], None)
    assert run.error is None and len(run.violations) == code


def test_ruff_error_is_unchecked_not_environment(fake_exec) -> None:
    fake_exec(ExecResult(2, "", "ruff failed: unknown field `foo`"))
    run = ruff_runner.run_ruff("c", ["a.py"], None)
    assert run.error is not None and "unknown field" in run.error


def test_docker_failure_is_environment_error(fake_exec) -> None:
    fake_exec(ExecResult(125, "", "daemon gone"))
    with pytest.raises(SandboxError):
        ruff_runner.run_ruff("c", ["a.py"], None)


def test_timeout_and_cancel(fake_exec) -> None:
    fake_exec(ExecResult(124, "", "", timed_out=True))
    assert "초 안에" in ruff_runner.run_ruff("c", ["a.py"], None).error
    fake_exec(ExecResult(None, "", "", cancelled=True))
    assert ruff_runner.run_ruff("c", ["a.py"], None).cancelled


def test_broken_json(fake_exec) -> None:
    fake_exec(ExecResult(1, "{nope", ""))
    assert "출력을 읽지 못함" in ruff_runner.run_ruff("c", ["a.py"], None).error


def _fix(applicability: str) -> dict:
    return {
        "applicability": applicability,
        "message": "Remove unused import",
        "edits": [
            {"content": "", "location": {"row": 1, "column": 1},
             "end_location": {"row": 2, "column": 1}},
        ],
    }  # fmt: skip


def test_only_safe_fixes_become_edits() -> None:
    safe = _item("a.py", 1) | {"fix": _fix("safe")}
    unsafe = _item("a.py", 2) | {"fix": _fix("unsafe")}
    display = _item("a.py", 3) | {"fix": _fix("display-only")}
    none = _item("a.py", 4) | {"fix": None}
    v_safe, v_unsafe, v_display, v_none = ruff_runner.parse_output(
        json.dumps([safe, unsafe, display, none])
    )
    assert v_safe.fixable and v_safe.edits[0].end_row == 2
    assert not v_unsafe.fixable and not v_display.fixable and not v_none.fixable


def test_format_argv() -> None:
    argv = ruff_runner.format_argv(["a.py"], None, check=True)
    assert argv[:2] == ["ruff", "format"] and "--check" in argv and "--isolated" in argv
    assert "--check" not in ruff_runner.format_argv(["a.py"], "c.toml", check=False)


@pytest.mark.parametrize(
    ("code", "check", "expected"),
    [(0, True, True), (1, True, False), (0, False, True)],
    ids=["formatted", "would-reformat", "applied"],
)
def test_format_exit_codes(fake_exec, code: int, check: bool, expected) -> None:
    fake_exec(ExecResult(code, "", ""))
    assert ruff_runner.run_format("c", ["."], None, check=check) is expected


@pytest.mark.parametrize("check", [True, False])
def test_format_error_is_reason_not_environment(fake_exec, check: bool) -> None:
    fake_exec(ExecResult(2, "", "error: Failed to parse a.py"))
    result = ruff_runner.run_format("c", ["a.py"], None, check=check)
    assert isinstance(result, str) and "Failed to parse" in result


def test_format_docker_failure_is_environment_error(fake_exec) -> None:
    fake_exec(ExecResult(125, "", "daemon gone"))
    with pytest.raises(SandboxError):
        ruff_runner.run_format("c", ["."], None, check=True)


def test_argv_select_overrides_rule_selection_only() -> None:
    argv = ruff_runner.ruff_argv(["a.py"], "cfg.toml", ("PLR2004", "PLW0603"))
    assert argv[argv.index("--select") + 1] == "PLR2004,PLW0603"
    assert argv[argv.index("--config") + 1] == "cfg.toml"  # per-file-ignores는 설정대로
    assert argv[-2:] == ["--", "a.py"]
    assert "--select" not in ruff_runner.ruff_argv(["a.py"], None)


def test_run_ruff_passes_select(fake_exec) -> None:
    calls = fake_exec(ExecResult(0, "[]", ""))
    ruff_runner.run_ruff("c", ["a.py"], None, select=("PLR2004",))
    assert "PLR2004" in calls[0]


def test_show_files_relative_paths(fake_exec) -> None:
    calls = fake_exec(ExecResult(0, "/work/b.py\n/work/pkg/a.py\n\n", ""))
    assert ruff_runner.run_show_files("c", ["pkg/a.py", "b.py", "gen/x.py"], "cfg.toml") == (
        "b.py",
        "pkg/a.py",
    )
    argv = calls[0]
    assert "--show-files" in argv and "--force-exclude" in argv
    assert argv[argv.index("--config") + 1] == "cfg.toml"


def test_show_files_no_files_skips_container(fake_exec) -> None:
    calls = fake_exec(ExecResult(0, "", ""))
    assert ruff_runner.run_show_files("c", [], None) == ()
    assert calls == []


def test_show_files_failures(fake_exec) -> None:
    fake_exec(ExecResult(2, "", "ruff failed: unknown field"))
    assert "unknown field" in ruff_runner.run_show_files("c", ["a.py"], None)
    fake_exec(ExecResult(2, "", ""))
    assert "종료 코드 2" in ruff_runner.run_show_files("c", ["a.py"], None)
    fake_exec(ExecResult(124, "", "", timed_out=True))
    assert "초 안에" in ruff_runner.run_show_files("c", ["a.py"], None)
    fake_exec(ExecResult(None, "", "", cancelled=True))
    assert ruff_runner.run_show_files("c", ["a.py"], None) is None
    fake_exec(ExecResult(125, "", "daemon gone"))
    with pytest.raises(SandboxError):
        ruff_runner.run_show_files("c", ["a.py"], None)
