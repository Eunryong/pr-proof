"""계층 규칙 (docs/CLAUDE.md "코드 구조", docs/DESIGN.md 12.5). import 문만 읽어 검사한다.

- 패키지는 app 하나이고 그 아래가 api(웹), worker(워커), shared(웹과 워커가 같이 쓰는 것) 셋이다
- app.api는 app.worker를, app.worker는 app.api를 import하지 않는다 (api 프로세스는 PR 코드를
  실행하지 않는다)
- app.shared는 app.api도 app.worker도 import하지 않는다
- app.worker.sandbox는 sandbox와 순수 모듈만 import한다
- 순수 모듈(이름이 `_pure`로 끝나는 모듈)은 저장소 안에서 다른 순수 모듈만 import하고, 금지 목록의
  라이브러리(파일 시스템, 프로세스, 네트워크, Redis, 큐, DB, 웹, 그래프, 벡터 저장소)를 import하지
  않는다. 함수 호출 자체는 보지 않는다
"""

import ast
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import pytest

import app

ROOT: Final = Path(__file__).resolve().parent.parent
TOP: Final = "app"
PACKAGES: Final = ("app.api", "app.worker", "app.shared")
SANDBOX: Final = "app.worker.sandbox"
PURE_SUFFIX: Final = "_pure"

# 패키지별로 import해도 되는 패키지
ALLOWED: Final[Mapping[str, frozenset[str]]] = {
    "app.api": frozenset({"app.api", "app.shared"}),
    "app.worker": frozenset({"app.worker", "app.shared"}),
    "app.shared": frozenset({"app.shared"}),
}
# 순수 모듈이 import하지 않는 표준·외부 라이브러리 (외부 호출, 프로세스, 파일 시스템)
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
            if top == TOP:
                inner.add(name)
            else:
                outer.add(top)
    return inner, outer


def package_of(name: str) -> str:
    """`app.worker.fix.decide` → `app.worker`. 맨 `app`은 그대로 `app`이다."""
    return ".".join(name.split(".")[:2])


def _sources(package: str) -> list[Path]:
    return sorted((ROOT / package.replace(".", "/")).rglob("*.py"))


def _found(checked: Mapping[Path, list[str]]) -> dict[str, list[str]]:
    return {str(p.relative_to(ROOT)): v for p, v in checked.items() if v}


def test_packages() -> None:
    top = {p.name for p in ROOT.iterdir() if (p / "__init__.py").is_file()} - {"tests"}
    assert top == {TOP}
    found = {f"{TOP}.{p.name}" for p in (ROOT / TOP).iterdir() if (p / "__init__.py").is_file()}
    assert found == set(PACKAGES) == set(ALLOWED)
    setuptools = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]
    assert setuptools["packages"]["find"]["include"] == [f"{TOP}*"]
    # 다른 체크아웃에 묶인 편집 가능 설치가 이 저장소의 app을 가리면 테스트가 옛 코드를 본다
    assert Path(app.__file__).resolve().parent == ROOT / TOP


@pytest.mark.parametrize("package", PACKAGES)
def test_package_imports(package: str) -> None:
    checked = {
        path: sorted({package_of(n) for n in imports_of(path)[0]} - ALLOWED[package])
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
    # 이름 규칙이 깨져 순수 모듈을 찾지 못하면 아래 검사가 아무것도 보지 않고 통과한다
    assert len(_pure_modules()) >= 37


@pytest.mark.parametrize("path", _pure_modules(), ids=lambda p: str(p.relative_to(ROOT)))
def test_pure_module_imports_only_pure_and_allowed(path: Path) -> None:
    inner, outer = imports_of(path)
    assert sorted(n for n in inner if not n.endswith(PURE_SUFFIX)) == []
    assert sorted(outer & PURE_FORBIDDEN) == []


def test_detects_violation(tmp_path: Path) -> None:
    for name in ("app", "app/worker", "app/worker/fix", "app/shared"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "__init__.py").write_text("")
    (tmp_path / "app" / "worker" / "state.py").write_text("")
    (tmp_path / "app" / "worker" / "fix" / "decide_pure.py").write_text("")
    bad = tmp_path / "app" / "worker" / "fix" / "bad_pure.py"
    bad.write_text(
        "import subprocess\nfrom app.api.sync import run_once\nfrom app.worker import state\n"
        "from . import decide_pure\nfrom app.shared.config import load_config\n"
    )
    inner, outer = imports_of(bad, tmp_path)
    assert inner == {
        "app.api.sync", "app.worker.state", "app.worker.fix.decide_pure", "app.shared.config"
    }  # fmt: skip
    assert outer == {"subprocess"}
    assert {package_of(n) for n in inner} == {"app.api", "app.worker", "app.shared"}
