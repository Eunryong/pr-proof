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


HIDE_PYTHON: Final = 'languageGlobs:\n  html: ["*.py"]\n'
# PR이 넣을 수 있는 ast-grep 설정. 파일 경로 → 내용
PLANTED_CONFIGS: Final = {
    "sgconfig at the root hides python": {"sgconfig.yml": HIDE_PYTHON},
    "sgconfig in the scanned folder": {"orders/sgconfig.yml": HIDE_PYTHON},
    "sgconfig with its own rule folder": {
        "sgconfig.yml": "ruleDirs:\n  - .ast-grep/rules\n",
        ".ast-grep/rules/extra.yml": (
            "id: planted\nlanguage: python\nseverity: error\nmessage: planted\n"
            "rule:\n  pattern: logger.info($$$A)\n"
        ),
    },
    "ast-grep folder alone": {
        ".ast-grep/sgconfig.yml": HIDE_PYTHON,
        ".ast-grep/rules/off.yml": (
            "id: no-print\nlanguage: python\nseverity: off\nmessage: x\n"
            "rule:\n  pattern: print($$$ARGS)\n"
        ),
    },
    "ignore file": {".gitignore": "orders/\n", ".ignore": "orders/\n", ".sgignore": "*.py\n"},
}


@pytest.mark.parametrize("planted", PLANTED_CONFIGS.values(), ids=PLANTED_CONFIGS.keys())
def test_config_planted_by_the_pr_does_not_change_the_result(
    repo: Path, tool_dir: Path, planted: dict[str, str]
) -> None:
    files = ["orders/bad.py", "orders/good.py"]
    before = checker_runner.run_checker("ast-grep", PRINT_RULE, repo, files, tool_dir=tool_dir)
    for rel, content in planted.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(content)
    after = checker_runner.run_checker("ast-grep", PRINT_RULE, repo, files, tool_dir=tool_dir)
    assert before == Ran((Match("orders/bad.py", 2, 2, "print를 쓰지 않는다"),))
    assert after == before


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


def test_script_that_ignores_a_large_file_list_finishes(repo: Path) -> None:
    files = [f"orders/m{i:05d}_{'x' * 40}.py" for i in range(3000)]  # 파이프 버퍼보다 큼
    run = checker_runner.run_checker("python-ast", "print('[]')\n", repo, files, timeout=30)
    assert run == Ran(())


TEST_NAME_DECL: Final = (
    '{"applies_to": "(^|/)tests/[^/]+\\\\.py$", "condition": "must_match", '
    '"pattern": "(^|/)(test_[^/]+|__init__|conftest)\\\\.py$", "message": "이름이 틀림"}'
)


def test_path_pattern_flags_paths_that_break_the_condition() -> None:
    paths = ["tests/pricing_test.py", "tests/test_pricing.py", "tests/__init__.py", "orders/a.py"]
    run = checker_runner.run_checker("path-pattern", TEST_NAME_DECL, Path("/nonexistent"), paths)
    assert run == Ran((Match("tests/pricing_test.py", 1, 1, "이름이 틀림"),))


def test_path_pattern_examples_are_matched_by_their_path() -> None:
    run = checker_runner.run_on_examples(
        "path-pattern", TEST_NAME_DECL, ["tests/checks.py\n", "tests/test_ok.py"]
    )
    assert isinstance(run, Ran)
    assert [m.file for m in run.matches] == [
        checker_runner.example_file("path-pattern", 0, "tests/checks.py\n")
    ]


def test_must_not_match_flags_matching_paths() -> None:
    decl = '{"applies_to": "", "condition": "must_not_match", "pattern": "[A-Z]"}'
    run = checker_runner.run_path_pattern(decl, ["orders/Pricing.py", "orders/pricing.py"])
    assert isinstance(run, Ran)
    assert [m.file for m in run.matches] == ["orders/Pricing.py"]


def test_backtracking_regex_is_stopped_by_the_time_limit() -> None:
    decl = '{"condition": "must_match", "pattern": "^(a+)+$"}'
    run = checker_runner.run_path_pattern(decl, ["a" * 60 + "!"], timeout=5)
    assert isinstance(run, Failed)


def test_path_pattern_that_is_not_a_declaration_never_runs() -> None:
    assert isinstance(checker_runner.run_path_pattern("import os", ["a.py"]), Invalid)
