"""app/worker/rules/doc_rules.py의 `read`: PR이 규칙 문서를 고쳤을 때 어느 커밋의 문서로 규칙을
읽는지. 로컬 git과 SQLite로 확인한다. 규칙 묶음은 저장해 두고 모델은 부르지 않는다."""

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest

from app.worker.config.repo_config_pure import CallSettings, ModelSettings, RulesSettings
from app.worker.core.rule_pure import CheckMethod, Rule, RuleSet, RuleSource, rule_id
from app.worker.git import repo
from app.worker.rules import doc_rules
from app.worker.rules.doc_rules_pure import NoRuleDocs, RulesRead
from app.worker.rules.rule_docs_source_pure import DocsSource, RuleChange
from app.worker.rules.rule_reading_pure import (
    PROMPT_VERSION,
    READER_VERSION,
    ruleset_key,
    select_documents,
)
from app.worker.store.budget import CallBudget
from app.worker.store.state import Store

from ...redis_stand_ins import MemoryKeys

NOW: Final = datetime(2026, 11, 18, 12, tzinfo=UTC)
MODELS: Final = ModelSettings()
OLD: Final = "`orders/` 아래 모듈은 `print`를 쓰지 않는다."
KEPT: Final = "함수 이름은 동사로 시작한다."
NEW: Final = "`orders/` 아래 모듈은 `print`를 쓰지 않는다. 로거를 쓴다."


class NoModel:
    """모델을 부르면 실패하는 모델 구현 자리."""

    @staticmethod
    def ask(prompt: str, **kwargs: object):
        pytest.fail("모델을 부르지 않아야 한다")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip


def _commit(path: Path, files: dict[str, str | None]) -> str:
    """files의 글을 쓰고(None이면 지우고) 커밋한 SHA."""
    for name, text in files.items():
        if text is None:
            (path / name).unlink()
        else:
            (path / name).write_text(text)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "--allow-empty", "-m", "c")
    return _git(path, "rev-parse", "HEAD")


def _readme(*rules: str) -> str:
    return "# demo\n\n## 코드 규칙\n\n" + "".join(f"- {r}\n" for r in rules)


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


def _rule(text: str) -> Rule:
    return Rule(id=rule_id("README.md", text), text=text,
                source=RuleSource("README.md", "코드 규칙", "x"), origin="document", scope=(),
                check=CheckMethod("checker"), fix="none")  # fmt: skip


def _save(worktree: Path, sha: str, store: Store, *texts: str) -> str:
    """sha의 규칙 문서를 읽은 규칙 묶음을 저장한다(규칙 검사가 전에 읽어 둔 것처럼). 그 키."""
    docs = RulesSettings().docs
    listed = select_documents(docs, repo.list_base_entries(worktree, sha, docs)).listed
    key = ruleset_key(listed, MODELS.rules)
    ruleset = RuleSet(
        rules=tuple(_rule(t) for t in texts),
        sources=tuple((e.path, e.sha) for e in listed),
        pipeline_version=READER_VERSION,
        prompt_version=PROMPT_VERSION,
        model="m",
    )
    store.save_ruleset(key, ruleset.to_json())
    return key


def _read(worktree: Path, base: str, store: Store, *, settings: RulesSettings | None = None,
          **kwargs):  # fmt: skip
    calls = kwargs.pop("calls", CallSettings())
    rt = doc_rules.Runtime(
        store=store, budget=CallBudget(store, "o/r", 1, "head", calls, now=lambda: NOW),
        models=MODELS, backend=NoModel(), sandbox=None,  # type: ignore[arg-type]
    )  # fmt: skip
    return doc_rules.read(worktree, base, settings or RulesSettings(), rt, **kwargs)


def _texts(read) -> list[str]:
    assert isinstance(read, RulesRead)
    return [r.text for r in read.rules]


@pytest.fixture
def edited(worktree: Path, store: Store) -> tuple[str, str, str, str]:
    """README의 규칙 하나를 고쳐 쓴 PR. (base, head, base의 키, head의 키)."""
    base = _commit(worktree, {"README.md": _readme(OLD, KEPT), "a.py": "x = 1\n"})
    head = _commit(worktree, {"README.md": _readme(NEW, KEPT)})
    base_key = _save(worktree, base, store, OLD, KEPT)
    return base, head, base_key, _save(worktree, head, store, NEW, KEPT)


def test_pr_that_leaves_the_documents_alone_reads_base_as_before(
    worktree: Path, store: Store
) -> None:
    base = _commit(worktree, {"README.md": _readme(OLD, KEPT), "a.py": "x = 1\n"})
    head = _commit(worktree, {"a.py": "x = 2\n"})
    key = _save(worktree, base, store, OLD, KEPT)
    before = _read(worktree, base, store)
    after = _read(worktree, base, store, head_sha=head, trusted=True)
    assert after == before and after.source is None and after.key == key
    hits = store.model_calls("o/r", 1)
    assert len(hits) == 2 and {c.ruleset_key for c in hits} == {key}


def test_documents_changed_on_base_after_the_pr_branched_are_not_the_pr_s_change(
    worktree: Path, store: Store
) -> None:
    """PR이 갈라진 뒤 base 쪽에서 규칙 문서가 바뀌었다. PR은 문서를 건드리지 않았으므로 믿는
    작성자의 PR이어도 base의 지금 문서로 읽는다."""
    fork = _commit(worktree, {"README.md": _readme(OLD, KEPT), "a.py": "x = 1\n"})
    _git(worktree, "switch", "-q", "-c", "feature")
    head = _commit(worktree, {"a.py": "x = 2\n"})
    _git(worktree, "switch", "-q", "main")
    base = _commit(worktree, {"README.md": _readme(NEW, KEPT)})
    assert fork != base
    key = _save(worktree, base, store, NEW, KEPT)
    read = _read(worktree, base, store, head_sha=head, fork_sha=fork, trusted=True)
    assert read.source is None and read.key == key and _texts(read) == [NEW, KEPT]


def test_trusted_author_is_checked_with_the_rules_of_the_pr(worktree, store, edited) -> None:
    base, head, _, head_key = edited
    read = _read(worktree, base, store, head_sha=head, trusted=True)
    assert _texts(read) == [NEW, KEPT] and read.key == head_key
    assert read.source == DocsSource(
        ("README.md",), "pr",
        removed=(RuleChange(rule_id("README.md", OLD), OLD),),
        added=(RuleChange(rule_id("README.md", NEW), NEW),),
    )  # fmt: skip


def test_untrusted_author_is_checked_with_the_rules_of_base(worktree, store, edited) -> None:
    base, head, base_key, _ = edited
    read = _read(worktree, base, store, head_sha=head, trusted=False)
    assert _texts(read) == [OLD, KEPT] and read.key == base_key
    assert read.source == DocsSource(("README.md",), "base", "author")


def test_setting_base_keeps_base_rules_for_a_trusted_author(worktree, store, edited) -> None:
    base, head, base_key, _ = edited
    settings = RulesSettings(docs_from="base")
    read = _read(worktree, base, store, settings=settings, head_sha=head, trusted=True)
    assert _texts(read) == [OLD, KEPT] and read.key == base_key
    assert read.source == DocsSource(("README.md",), "base", "setting")


def test_document_added_by_the_pr_is_read_and_all_its_rules_are_new(worktree, store) -> None:
    base = _commit(worktree, {"a.py": "x = 1\n"})
    head = _commit(worktree, {"README.md": _readme(KEPT)})
    _save(worktree, head, store, KEPT)
    read = _read(worktree, base, store, head_sha=head, trusted=True)
    assert _texts(read) == [KEPT]
    assert read.source == DocsSource(
        ("README.md",), "pr", added=(RuleChange(rule_id("README.md", KEPT), KEPT),)
    )
    assert _read(worktree, base, store, head_sha=head) == NoRuleDocs(
        source=DocsSource(("README.md",), "base", "author")
    )


def test_document_removed_by_the_pr_leaves_no_rules_and_lists_them_as_removed(
    worktree, store
) -> None:
    base = _commit(worktree, {"README.md": _readme(KEPT), "a.py": "x = 1\n"})
    head = _commit(worktree, {"README.md": None})
    _save(worktree, base, store, KEPT)
    read = _read(worktree, base, store, head_sha=head, trusted=True)
    assert read == NoRuleDocs(
        source=DocsSource(("README.md",), "pr",
                          removed=(RuleChange(rule_id("README.md", KEPT), KEPT),))
    )  # fmt: skip


def test_comparison_is_skipped_when_base_rules_cannot_be_read(worktree, store) -> None:
    base = _commit(worktree, {"README.md": _readme(OLD), "a.py": "x = 1\n"})
    head = _commit(worktree, {"README.md": _readme(NEW)})
    _save(worktree, head, store, NEW)  # base의 규칙 묶음은 저장돼 있지 않고 호출 상한이 0이다
    read = _read(worktree, base, store, head_sha=head, trusted=True,
                 calls=CallSettings(ruleset_base=0))  # fmt: skip
    assert _texts(read) == [NEW]
    assert read.source is not None and read.source.read_from == "pr"
    assert (read.source.removed, read.source.added) == ((), ())
    assert read.source.compare_skipped.startswith("규칙을 읽지 않음: ")
