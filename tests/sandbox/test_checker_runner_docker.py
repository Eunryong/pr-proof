"""검사기 실행 통합 테스트: 실제 ast-grep과 샌드박스 컨테이너."""

from pathlib import Path
from typing import Final

import pytest

from app.sandbox import checker_runner, toolcache
from app.sandbox.checker_runner import Failed, Invalid, Match, Ran

pytestmark = pytest.mark.docker

PRINT_RULE: Final = """\
id: no-print
language: python
severity: hint
message: print를 쓰지 않는다
rule:
  pattern: print($$$ARGS)
"""
GOOD: Final = "import logging\n\nlogger = logging.getLogger(__name__)\nlogger.info('x')\n"
BAD: Final = "def f(order):\n    print('coupon', order)\n    return 1\n"


@pytest.fixture(scope="module")
def tool_dir(tool_cache: Path) -> Path:
    return toolcache.ensure("ast-grep", cache_dir=tool_cache)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "orders").mkdir()
    (tmp_path / "orders" / "bad.py").write_text(BAD)
    (tmp_path / "orders" / "good.py").write_text(GOOD)
    return tmp_path


def test_checker_flags_bad_example_line_and_not_good(tool_dir: Path) -> None:
    run = checker_runner.run_on_examples("ast-grep", PRINT_RULE, [GOOD, BAD], tool_dir=tool_dir)
    assert run == Ran((Match(checker_runner.example_path(1), 2, 2, "print를 쓰지 않는다"),))


def test_wrong_checker_misses_the_violation(tool_dir: Path) -> None:
    wrong = PRINT_RULE.replace("print($$$ARGS)", "pprint($$$ARGS)")
    assert checker_runner.run_on_examples("ast-grep", wrong, [BAD], tool_dir=tool_dir) == Ran(())


def test_only_given_files_are_scanned(repo: Path, tool_dir: Path) -> None:
    run = checker_runner.run_checker(
        "ast-grep", PRINT_RULE, repo, ["orders/good.py"], tool_dir=tool_dir
    )
    assert run == Ran(())


def test_repo_sgconfig_cannot_hide_matches(repo: Path, tool_dir: Path) -> None:
    (repo / "sgconfig.yml").write_text('languageGlobs:\n  html: ["*.py"]\n')
    run = checker_runner.run_checker("ast-grep", PRINT_RULE, repo, ["orders/bad.py"],
                                     tool_dir=tool_dir)  # fmt: skip
    assert isinstance(run, Ran) and [m.line for m in run.matches] == [2]


def test_invalid_rule_returns_engine_message(repo: Path, tool_dir: Path) -> None:
    body = PRINT_RULE.replace("pattern:", "patern:")
    run = checker_runner.run_checker("ast-grep", body, repo, ["orders/bad.py"], tool_dir=tool_dir)
    assert isinstance(run, Invalid) and "patern" in run.reason


def test_fix_rule_is_rejected_before_running(repo: Path, tool_dir: Path) -> None:
    body = PRINT_RULE + "fix: logger.info($$$ARGS)\n"
    run = checker_runner.run_checker("ast-grep", body, repo, ["orders/bad.py"], tool_dir=tool_dir)
    assert isinstance(run, Invalid) and "fix" in run.reason
    assert (repo / "orders" / "bad.py").read_text() == BAD


def test_worktree_is_read_only(repo: Path) -> None:
    script = "open('orders/bad.py', 'w').write('x')\nopen('new.py', 'w').write('x')\n"
    run = checker_runner.run_checker("python-ast", script, repo, ["orders/bad.py"])
    assert isinstance(run, Invalid) and "Read-only file system" in run.reason
    assert (repo / "orders" / "bad.py").read_text() == BAD
    assert not (repo / "new.py").exists()


def test_run_container_has_no_network(repo: Path) -> None:
    script = "import socket\nsocket.create_connection(('1.1.1.1', 443), timeout=5)\n"
    run = checker_runner.run_checker("python-ast", script, repo, ["orders/bad.py"])
    assert isinstance(run, Invalid) and "unreachable" in run.reason.lower(), run


def test_python_ast_script_contract(repo: Path) -> None:
    script = """\
import ast, json, sys
out = []
for path in json.load(sys.stdin)["files"]:
    for node in ast.walk(ast.parse(open(path).read())):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "print":
            out.append({"file": path, "line": node.lineno, "why": "print"})
print(json.dumps(out))
"""
    run = checker_runner.run_checker(
        "python-ast", script, repo, ["orders/bad.py", "orders/good.py"]
    )
    assert run == Ran((Match("orders/bad.py", 2, 2, "print"),))


def test_runaway_checker_is_stopped(repo: Path) -> None:
    run = checker_runner.run_checker("python-ast", "while True:\n    pass\n", repo,
                                     ["orders/bad.py"], timeout=3)  # fmt: skip
    assert run == Failed("3초 안에 끝나지 않음")
