"""코드 규칙: 모듈 수준 상수(대문자 이름)는 typing.Final로 선언한다."""

import ast
import re
from pathlib import Path
from typing import Final

import pytest

ROOT: Final = Path(__file__).resolve().parent.parent
CONSTANT_NAME: Final = re.compile(r"^_?[A-Z][A-Z0-9_]*$")


def _is_final(annotation: ast.expr) -> bool:
    node = annotation.value if isinstance(annotation, ast.Subscript) else annotation
    return (isinstance(node, ast.Name) and node.id == "Final") or (
        isinstance(node, ast.Attribute) and node.attr == "Final"
    )


def constants_without_final(source: str) -> list[str]:
    """모듈 최상위에서 Final 없이 대입한 대문자 이름."""
    missing = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            missing += [n for n in names if CONSTANT_NAME.match(n)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
            if CONSTANT_NAME.match(name) and not _is_final(node.annotation):
                missing.append(name)
    return missing


@pytest.mark.parametrize(
    "path",
    sorted([*(ROOT / "app").rglob("*.py"), *(ROOT / "tests").rglob("*.py")]),
    ids=lambda p: str(p.relative_to(ROOT)),
)
def test_constants_are_final(path: Path) -> None:
    assert constants_without_final(path.read_text()) == []


@pytest.mark.parametrize(
    ("source", "missing"),
    [
        ("X = 1\n", ["X"]),
        ("_PRIVATE = 1\n", ["_PRIVATE"]),
        ("X: int = 1\n", ["X"]),
        ("X: Final = 1\n", []),
        ("X: Final[int] = 1\n", []),
        ("X: typing.Final[int] = 1\n", []),
        ("x = 1\nValue = 2\n", []),  # 대문자 상수가 아님
        ("def f():\n    X = 1\n", []),  # 함수 안 지역 변수
    ],
)
def test_detects_missing_final(source: str, missing: list[str]) -> None:
    assert constants_without_final(source) == missing
