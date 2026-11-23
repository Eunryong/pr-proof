"""위반 찾기 통합 테스트: 실제 ast-grep 검사기를 worktree에 돌린다."""

from dataclasses import replace
from pathlib import Path
from typing import Final

import pytest

from app.worker.diff_pure import added_lines
from app.worker.rules.checkers import Sandbox
from app.worker.rules.detect import detect

from .test_detect import CHECKED, CHECKER, _diff

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
def worktree(tmp_path: Path) -> Path:
    services = tmp_path / "orders" / "services"
    services.mkdir(parents=True)
    (services / "pricing.py").write_text(
        "def total(o):\n    print('base')\n    x = 1\n    print('pr')\n    return x\n"
    )
    return tmp_path


@pytest.mark.parametrize(("kind", "body"), [("ast-grep", PRINT_RULE), ("python-ast", PRINT_SCRIPT)])
def test_real_checker_reports_only_the_changed_line(
    worktree: Path, tool_cache: Path, sandbox_image: str, kind: str, body: str
) -> None:
    check = replace(CHECKED, checker=replace(CHECKER, kind=kind, body=body))
    changed = added_lines(_diff("orders/services/pricing.py", 4, 1))
    result = detect([check], worktree, changed, sandbox=Sandbox(tool_cache, image=sandbox_image))
    assert [(v.file, v.line, v.line_text) for v in result.violations] == [
        ("orders/services/pricing.py", 4, "print('pr')")
    ]
    assert result.unchecked == ()
