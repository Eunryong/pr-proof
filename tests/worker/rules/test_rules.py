"""app/worker/rules/rules.py: base의 문서로 규칙 묶음을 만들 때 모델을 부르지 않는 길. 로컬 git,
SQLite로 확인한다."""

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest

from app.worker import repo
from app.worker.budget import CallBudget
from app.worker.call_budget_pure import Denied
from app.worker.repo_config_pure import CallSettings, RulesSettings
from app.worker.rules.rules import CapReached, NoRuleDocuments, load_rules
from app.worker.state import Store

from ...redis_stand_ins import MemoryKeys

README: Final = (Path(__file__).parents[2] / "fixtures" / "rules" / "README.md").read_text()
NOW: Final = datetime(2026, 11, 18, 12, tzinfo=UTC)


def _no_model(prompt: str, **kwargs: object):
    pytest.fail("모델을 부르지 않아야 한다")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip


def _commit(path: Path, files: dict[str, str | bytes]) -> str:
    for name, content in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "--allow-empty", "-m", "c")
    return _git(path, "rev-parse", "HEAD")


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    path = tmp_path / "wt"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    repo.record_git_dir(path)
    return path


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "pipeline.db", MemoryKeys())


def _load(worktree: Path, base: str, store: Store, *, pr: int = 1,
          settings: RulesSettings | None = None, **calls: int):  # fmt: skip
    budget = CallBudget(store, "o/r", pr, "head", CallSettings(**calls), now=lambda: NOW)
    return load_rules(worktree, base, settings or RulesSettings(), model="haiku", store=store,
                      budget=budget, ask=_no_model)  # fmt: skip


def test_no_rule_documents_calls_no_model(worktree: Path, store: Store) -> None:
    base = _commit(worktree, {"app.py": "x = 1\n", "sub/README.md": README})
    assert _load(worktree, base, store) == NoRuleDocuments(unread=())
    assert _load(worktree, base, store, settings=RulesSettings(docs=[])) == NoRuleDocuments(())
    assert store.model_calls("o/r", 1) == []


def test_symlinked_readme_is_not_read(worktree: Path, store: Store) -> None:
    (worktree / "notes.md").write_text(README)
    os.symlink("notes.md", worktree / "README.md")
    base = _commit(worktree, {})
    result = _load(worktree, base, store)
    assert isinstance(result, NoRuleDocuments)
    assert [u.path for u in result.unread] == ["README.md"]


def test_ruleset_cap_stops_reading(worktree: Path, store: Store) -> None:
    base = _commit(worktree, {"README.md": README})
    result = _load(worktree, base, store, ruleset_base=0)
    assert isinstance(result, CapReached) and result.denied == Denied("ruleset", 0, 0, 1)
    assert store.model_calls("o/r", 1) == []


# --- 400KB를 넘는 규칙 문서 (DESIGN.md 6.3) ---

ORDER_RULE: Final = "`orders/` 아래 모듈은 `print`를 쓰지 않는다."
STYLE_RULE: Final = "함수 이름은 동사로 시작한다."
PAY_RULE: Final = "`payments/` 아래 모듈은 금액을 `Decimal`로 다룬다."
FILLER: Final = "이 절은 규칙이 아닌 설명이다. " * 40


def _big_readme(parts: int = 400) -> str:
    """앞에 머리와 코드 규칙 절, 가운데 설명 절이 많고, 주문·결제 절에 규칙이 하나씩 있는 문서."""
    filler = "".join(f"## 설명 {n}\n\n{FILLER}\n\n" for n in range(parts))
    return (
        "# 큰 안내서\n\n머리 글\n\n## 코드 규칙\n\n- " + STYLE_RULE + "\n\n" + filler
        + "## 주문 모듈\n\n- " + ORDER_RULE + "\n\n## 결제 모듈\n\n- " + PAY_RULE + "\n"
    )  # fmt: skip


ORDER_DIFF: Final = (
    b"diff --git a/orders/a.py b/orders/a.py\n--- a/orders/a.py\n+++ b/orders/a.py\n"
    b"@@ -1 +1,2 @@\n x = 1\n+print(x)\n"
)


def _load_big(worktree: Path, base: str, store: Store, sections):
    budget = CallBudget(store, "o/r", 1, "head", CallSettings(), now=lambda: NOW)
    return load_rules(worktree, base, RulesSettings(), model="haiku", store=store, budget=budget,
                      ask=_no_model, sections=sections, changes_diff=ORDER_DIFF)  # fmt: skip


def test_big_document_without_search_reads_nothing_beyond_the_limit(
    worktree: Path, store: Store
) -> None:
    base = _commit(worktree, {"README.md": _big_readme()})
    result = _load_big(worktree, base, store, None)
    assert isinstance(result, NoRuleDocuments)
    assert "크기 상한을 넘음" in result.unread[0].reason


def test_failed_section_search_falls_back_to_the_size_limit(worktree: Path, store: Store) -> None:
    class Broken:
        def find_sections(self, chunks, query: str):
            raise RuntimeError("chroma 실패")

    base = _commit(worktree, {"README.md": _big_readme()})
    result = _load_big(worktree, base, store, Broken())
    assert isinstance(result, NoRuleDocuments)
