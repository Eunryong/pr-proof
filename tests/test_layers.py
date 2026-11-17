"""계층 규칙.

- 웹(api) 쪽은 MVC: controllers → services·views → models
- 워커: worker → sandbox → models
- 웹 쪽은 sandbox·worker를 import하지 않는다 (api 프로세스는 PR 코드를 실행하지 않는다)
"""

import ast
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import pytest

APP: Final = Path(__file__).resolve().parent.parent / "app"

# 계층별로 import해도 되는 app 내부 계층
ALLOWED: Final[Mapping[str, frozenset[str]]] = {
    "models": frozenset({"models"}),
    "services": frozenset({"models", "services"}),
    "views": frozenset({"models", "views"}),
    "controllers": frozenset({"models", "services", "views", "controllers"}),
    "sandbox": frozenset({"models", "sandbox"}),
    "worker": frozenset({"models", "sandbox", "worker"}),
}


def test_every_package_has_rule() -> None:
    packages = {p.name for p in APP.iterdir() if (p / "__init__.py").is_file()}
    assert packages == set(ALLOWED)


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
