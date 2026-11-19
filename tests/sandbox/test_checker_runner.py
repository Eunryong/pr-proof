"""app/sandbox/checker_runner.py: 출력 읽기와 실행 전 거절. Docker 없이 돈다."""

import json
from pathlib import Path
from typing import Final

import pytest

from app.sandbox import checker_runner
from app.sandbox.checker_runner import Failed, Invalid, Match, Ran, RawRun
from app.sandbox.docker_cli import SandboxError

FILES: Final = frozenset({"orders/a.py", "orders/b.py"})


def _ast_grep_item(file: str, start: int, end: int | None = None) -> dict:
    return {
        "file": file, "message": "m",
        "range": {"start": {"line": start}, "end": {"line": start if end is None else end}},
    }  # fmt: skip


def _read(kind: str, stdout: object, returncode: int = 1, **kwargs: object) -> object:
    raw = RawRun(returncode, json.dumps(stdout).encode(), "", **kwargs)
    return checker_runner.read_result(kind, raw, FILES, 120)  # type: ignore[arg-type]


def test_ast_grep_lines_become_one_based_and_relative() -> None:
    run = _read("ast-grep", [_ast_grep_item("/work/orders/a.py", 0, 2)])
    assert run == Ran((Match("orders/a.py", 1, 3, "m"),))


def test_clean_run_has_no_matches() -> None:
    assert _read("ast-grep", [], returncode=0) == Ran(())


@pytest.mark.parametrize(
    "file", ["/etc/passwd", "/work/../etc/passwd", "orders/a.py", "/work/orders/c.py"]
)
def test_ast_grep_output_outside_given_files_is_discarded(file: str) -> None:
    assert isinstance(_read("ast-grep", [_ast_grep_item(file, 0)]), Failed)


def test_script_output_contract() -> None:
    run = _read("python-ast", [{"file": "orders/b.py", "line": 4, "why": "w"}], returncode=0)
    assert run == Ran((Match("orders/b.py", 4, 4, "w"),))


@pytest.mark.parametrize(
    "stdout",
    [
        {"file": "orders/a.py", "line": 1},
        [{"file": "../x.py", "line": 1}],
        [{"file": "orders/z.py", "line": 1}],
        [{"file": "orders/a.py", "line": 0}],
        [{"file": "orders/a.py", "line": "3"}],
        [{"file": "orders/a.py", "line": True}],
    ],
)
def test_script_breaking_its_contract_is_invalid(stdout: object) -> None:
    assert isinstance(_read("python-ast", stdout, returncode=0), Invalid)


def test_engine_error_is_invalid_with_stderr() -> None:
    raw = RawRun(8, b"", "Error: Cannot parse rule\nunknown field `patern`")
    run = checker_runner.read_result("ast-grep", raw, FILES, 120)
    assert run == Invalid("Error: Cannot parse rule\nunknown field `patern`")


def test_limits_and_docker_failures() -> None:
    timed_out = _read("ast-grep", [], timed_out=True, returncode=None)
    assert timed_out == Failed("120초 안에 끝나지 않음")
    assert isinstance(_read("ast-grep", [], too_large=True), Failed)
    assert isinstance(_read("ast-grep", [], returncode=137), Failed)
    with pytest.raises(SandboxError):
        _read("ast-grep", [], returncode=125)


def test_fix_rule_is_rejected_without_docker(tmp_path: Path) -> None:
    body = "id: x\nlanguage: python\nrule: {pattern: print($A)}\nfix: log($A)\n"
    run = checker_runner.run_checker(
        "ast-grep", body, tmp_path, ["a.py"], tool_dir=tmp_path, image="unused"
    )
    assert isinstance(run, Invalid) and "fix" in run.reason


@pytest.mark.parametrize("path", ["/abs.py", "../up.py", "a/../../b.py", ""])
def test_input_paths_must_be_relative_inside(tmp_path: Path, path: str) -> None:
    with pytest.raises(ValueError):
        checker_runner.run_checker(
            "python-ast", "x = 1\n", tmp_path, [path], tool_dir=tmp_path, image="unused"
        )
