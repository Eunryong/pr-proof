"""app/models/checker.py: 실행 전 검사기 본문 확인."""

from typing import Final

import pytest

from app.models.checker import checker_id, validate_body

PRINT_RULE: Final = """\
id: no-print
language: python
severity: error
message: print를 쓰지 않는다
rule:
  pattern: print($$$ARGS)
"""


def test_valid_rule_passes() -> None:
    assert validate_body("ast-grep", PRINT_RULE) is None


def test_local_utils_may_be_referenced() -> None:
    body = PRINT_RULE.replace(
        "rule:\n  pattern: print($$$ARGS)\n",
        "utils:\n  is-print:\n    pattern: print($$$A)\nrule:\n  any:\n    - matches: is-print\n",
    )
    assert validate_body("ast-grep", body) is None


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("", "비어 있음"),
        ("x" * 20_001, "자를 넘음"),
        (PRINT_RULE + "fix: logger.info($$$ARGS)\n", "쓸 수 없는 키: fix"),
        (PRINT_RULE + "rewriters: []\n", "쓸 수 없는 키: rewriters"),
        (PRINT_RULE + "transform: {}\n", "쓸 수 없는 키: transform"),
        (PRINT_RULE + "files: ['orders/**']\n", "쓸 수 없는 키: files"),
        (PRINT_RULE + "ignores: ['../**']\n", "쓸 수 없는 키: ignores"),
        (PRINT_RULE + "---\n" + PRINT_RULE, "문서는 하나여야 함 (2개)"),
        (PRINT_RULE.replace("language: python", "language: javascript"), "language는 python"),
        (PRINT_RULE.replace("language: python\n", ""), "language는 python"),
        ("- a\n- b\n", "매핑이 아님"),
        ("id: x\nlanguage: python\nrule: [\n", "YAML을 읽지 못함"),
        ("id: x\nlanguage: python\nmessage: m\n", "rule이 없거나"),
        (PRINT_RULE.replace("pattern: print($$$ARGS)", "matches: shared-util"), "shared-util"),
        ("id: x\nlanguage: python\nrule: &r {pattern: a}\nnote: *r\n", "별칭"),
    ],
)
def test_rejects_before_running(body: str, reason: str) -> None:
    problem = validate_body("ast-grep", body)
    assert problem is not None and reason in problem, problem


def test_python_script_may_use_stdlib_only() -> None:
    assert validate_body("python-ast", "import ast, json, sys\nfrom pathlib import Path\n") is None
    assert "requests" in validate_body("python-ast", "import requests\n")
    assert "." in validate_body("python-ast", "from . import x\n")
    assert "문법 오류" in validate_body("python-ast", "def (:\n")


def test_checker_id_is_body_hash() -> None:
    assert checker_id("a") == checker_id("a") != checker_id("b")
