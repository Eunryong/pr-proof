"""검사기 작성 통합 테스트: 가짜 모델의 답을 실제 ast-grep·샌드박스로 예시 확인한다."""

from pathlib import Path
from typing import Final

import fakeredis
import pytest

from app.models.checker_authoring import Checked
from app.worker.checkers import Sandbox
from app.worker.state import Store

from .test_checkers import FakeModel, _author, _reply

pytestmark = pytest.mark.docker

PRINT_RULE: Final = """\
id: no-print
language: python
severity: error
message: print를 쓰지 않는다
rule:
  pattern: print($$$ARGS)
"""
PRINT_SCRIPT: Final = """\
import ast, json, sys
out = []
for path in json.load(sys.stdin)["files"]:
    try:
        tree = ast.parse(open(path).read())
    except SyntaxError:
        continue
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "print":
            out.append({"file": path, "line": node.lineno, "why": "print"})
print(json.dumps(out))
"""


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "pipeline.db", fakeredis.FakeStrictRedis())


def test_real_checker_passing_examples_is_accepted(store: Store, tool_cache: Path) -> None:
    model = FakeModel(_reply(checker=PRINT_RULE))
    (result,) = _author(store, model, Sandbox(tool_cache))
    assert isinstance(result, Checked) and result.checker.kind == "ast-grep"


def test_real_wrong_checker_fails_an_example_and_is_retried(store: Store, tool_cache: Path) -> None:
    wrong = PRINT_RULE.replace("print($$$ARGS)", "pprint($$$ARGS)")
    model = FakeModel(_reply(checker=wrong), _reply(checker=PRINT_RULE))
    (result,) = _author(store, model, Sandbox(tool_cache))
    assert isinstance(result, Checked)
    assert "위반 예시 1 (문서): 2번째 줄에서 잡혀야 함, 실제로는 잡히지 않음." in model.calls[1][0]


def test_real_fix_rule_is_rejected_before_running(store: Store, tool_cache: Path) -> None:
    model = FakeModel(_reply(checker=PRINT_RULE + "fix: logger.info($$$ARGS)\n"),
                      _reply(checker=PRINT_RULE))  # fmt: skip
    (result,) = _author(store, model, Sandbox(tool_cache))
    assert isinstance(result, Checked)
    assert "쓸 수 없는 키: fix" in model.calls[1][0]


def test_third_attempt_switches_to_python_script(store: Store, tool_cache: Path) -> None:
    wrong = _reply(checker=PRINT_RULE.replace("print(", "pprint("))
    model = FakeModel(wrong, wrong, _reply(checker=PRINT_SCRIPT))
    (result,) = _author(store, model, Sandbox(tool_cache))
    assert isinstance(result, Checked) and result.checker.kind == "python-ast"
