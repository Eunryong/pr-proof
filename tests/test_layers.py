"""계층 규칙 (docs/CLAUDE.md "코드 구조", docs/DESIGN.md 12.1).

- 최상위 패키지는 api(웹), worker(워커), shared(웹과 워커가 같이 쓰는 것) 셋이다
- api는 worker를, worker는 api를 import하지 않는다 (api 프로세스는 PR 코드를 실행하지 않는다)
- shared는 api도 worker도 import하지 않는다
- worker/sandbox는 sandbox와 판단 규칙만 import한다
- 판단 규칙은 이름이 `_pure`로 끝나는 모듈이고 외부 호출이 없다. 다른 판단 규칙만 import하고,
  실행 코드 쪽 라이브러리(subprocess, Redis, 큐, 웹, 그래프, 벡터 저장소 등)를 import하지 않는다
"""

import ast
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import pytest

ROOT: Final = Path(__file__).resolve().parent.parent
PACKAGES: Final = ("api", "worker", "shared")
SANDBOX: Final = "worker.sandbox"
PURE_SUFFIX: Final = "_pure"

# 최상위 패키지별로 import해도 되는 최상위 패키지
ALLOWED: Final[Mapping[str, frozenset[str]]] = {
    "api": frozenset({"api", "shared"}),
    "worker": frozenset({"worker", "shared"}),
    "shared": frozenset({"shared"}),
}
# 판단 규칙이 import하지 않는 표준·외부 라이브러리 (외부 호출, 프로세스, 파일 시스템)
PURE_FORBIDDEN: Final = frozenset(
    {
        "os", "shutil", "tempfile", "subprocess", "socket", "http", "urllib", "sqlite3",
        "redis", "rq", "httpx", "requests", "fastapi", "uvicorn", "langgraph", "chromadb",
        "onnxruntime", "tokenizers",
    }
)  # fmt: skip


def _module_of(path: Path, root: Path) -> list[str]:
    parts = list(path.relative_to(root).with_suffix("").parts)
    return parts[:-1] if parts[-1] == "__init__" else parts


def _is_module(name: str, root: Path) -> bool:
    path = root.joinpath(*name.split("."))
    return path.with_suffix(".py").is_file() or (path / "__init__.py").is_file()


def imports_of(path: Path, root: Path = ROOT) -> tuple[set[str], set[str]]:
    """(import하는 저장소 안의 모듈, import하는 바깥 라이브러리의 최상위 이름)."""
    inner, outer = set(), set()
    here = _module_of(path, root)
    if path.name != "__init__.py":
        here = here[:-1]
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            targets = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = ".".join([*here[: len(here) - node.level + 1], *filter(None, [base])])
            targets = [
                f"{base}.{a.name}" if _is_module(f"{base}.{a.name}", root) else base
                for a in node.names
            ]
        else:
            continue
        for name in targets:
            top = name.split(".")[0]
            if top in PACKAGES:
                inner.add(name)
            else:
                outer.add(top)
    return inner, outer


def _sources(package: str) -> list[Path]:
    return sorted((ROOT / package.replace(".", "/")).rglob("*.py"))


def _found(checked: Mapping[Path, list[str]]) -> dict[str, list[str]]:
    return {str(p.relative_to(ROOT)): v for p, v in checked.items() if v}


def test_top_level_packages() -> None:
    found = {p.name for p in ROOT.iterdir() if (p / "__init__.py").is_file()} - {"tests"}
    assert found == set(PACKAGES) == set(ALLOWED)
    setuptools = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]
    assert sorted(setuptools["packages"]["find"]["include"]) == sorted(f"{p}*" for p in PACKAGES)


@pytest.mark.parametrize("package", PACKAGES)
def test_package_imports(package: str) -> None:
    checked = {
        path: sorted({n.split(".")[0] for n in imports_of(path)[0]} - ALLOWED[package])
        for path in _sources(package)
    }
    assert _found(checked) == {}


def test_sandbox_imports_only_sandbox_and_pure() -> None:
    def allowed(name: str) -> bool:
        return name == SANDBOX or name.startswith(SANDBOX + ".") or name.endswith(PURE_SUFFIX)

    checked = {
        path: sorted(n for n in imports_of(path)[0] if not allowed(n)) for path in _sources(SANDBOX)
    }
    assert _found(checked) == {}


def _pure_modules() -> list[Path]:
    return [p for package in PACKAGES for p in _sources(package) if p.stem.endswith(PURE_SUFFIX)]


def test_pure_modules_are_found() -> None:
    # 이름 규칙이 깨져 판단 규칙을 찾지 못하면 아래 검사가 아무것도 보지 않고 통과한다
    assert len(_pure_modules()) >= 37


@pytest.mark.parametrize("path", _pure_modules(), ids=lambda p: str(p.relative_to(ROOT)))
def test_pure_module_has_no_external_calls(path: Path) -> None:
    inner, outer = imports_of(path)
    assert sorted(n for n in inner if not n.endswith(PURE_SUFFIX)) == []
    assert sorted(outer & PURE_FORBIDDEN) == []


def test_detects_violation(tmp_path: Path) -> None:
    for name in ("worker", "worker/fix", "shared"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "__init__.py").write_text("")
    (tmp_path / "worker" / "state.py").write_text("")
    (tmp_path / "worker" / "fix" / "decide_pure.py").write_text("")
    bad = tmp_path / "worker" / "fix" / "bad_pure.py"
    bad.write_text(
        "import subprocess\nfrom api.sync import run_once\nfrom worker import state\n"
        "from . import decide_pure\nfrom shared.config import load_config\n"
    )
    inner, outer = imports_of(bad, tmp_path)
    assert inner == {"api.sync", "worker.state", "worker.fix.decide_pure", "shared.config"}
    assert outer == {"subprocess"}
