"""MVC 계층 규칙: controllers → services·views → models 한 방향으로만 import한다."""

import ast
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "app"

# 계층별로 import해도 되는 app 내부 계층
ALLOWED = {
    "models": {"models"},
    "services": {"models", "services"},
    "views": {"models", "views"},
    "controllers": {"models", "services", "views", "controllers"},
}


def _app_imports(path: Path) -> set[str]:
    """파일이 import하는 app 내부 계층 이름."""
    layers = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
        elif isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        else:
            continue
        for name in names:
            parts = name.split(".")
            if parts[0] == "app" and len(parts) > 1:
                layers.add(parts[1])
    return layers


@pytest.mark.parametrize("layer", sorted(ALLOWED))
def test_layer_imports(layer: str) -> None:
    violations = {
        str(path.relative_to(APP)): sorted(_app_imports(path) - ALLOWED[layer])
        for path in (APP / layer).rglob("*.py")
    }
    assert {k: v for k, v in violations.items() if v} == {}


def test_detects_violation(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(
        "from app.controllers.test_run import run_pr_tests\nimport app.services.sandbox\n"
    )
    assert _app_imports(bad) == {"controllers", "services"}
