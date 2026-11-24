"""app/worker/rules/rule_docs_source_pure.py: PR이 규칙 문서를 고쳤을 때 읽는 커밋과 바뀐 규칙."""

from typing import Final

import pytest

from app.worker.core.rule_pure import CheckMethod, Rule, RuleSource, rule_id
from app.worker.git.git_pure import TreeEntry
from app.worker.rules.rule_docs_source_pure import (
    DocsSource,
    RuleChange,
    changed_docs,
    compare,
    read_commit,
    source_for,
    with_comparison,
)

DOCS: Final = ("README.md", "CONTRIBUTING.md")
BASE_SHA: Final = "b" * 40
HEAD_SHA: Final = "a" * 40


def _entry(path: str, sha: str = "1" * 40, mode: str = "100644") -> TreeEntry:
    return TreeEntry(path, mode, sha, 10)


def _entries(*entries: TreeEntry) -> dict[str, TreeEntry]:
    return {e.path: e for e in entries}


def _rule(text: str, doc: str = "README.md") -> Rule:
    return Rule(id=rule_id(doc, text), text=text, source=RuleSource(doc, "코드 규칙", "x"),
                origin="document", scope=(), check=CheckMethod("checker"), fix="none")  # fmt: skip


SAME: Final = _entries(_entry("README.md"), _entry("CONTRIBUTING.md"))
EDITED: Final = _entries(_entry("README.md", sha="2" * 40), _entry("CONTRIBUTING.md"))


@pytest.mark.parametrize("trusted", [True, False])
@pytest.mark.parametrize("rules_from", ["pr", "base"])
def test_unchanged_documents_are_read_from_base(trusted: bool, rules_from) -> None:
    source = source_for(DOCS, SAME, SAME, trusted=trusted, rules_from=rules_from)
    assert source is None
    assert read_commit(source, BASE_SHA, HEAD_SHA) == BASE_SHA


def test_trusted_author_with_default_setting_reads_the_pr() -> None:
    source = source_for(DOCS, SAME, EDITED, trusted=True, rules_from="pr")
    assert source == DocsSource(("README.md",), "pr")
    assert read_commit(source, BASE_SHA, HEAD_SHA) == HEAD_SHA


def test_untrusted_or_unknown_author_reads_base() -> None:
    source = source_for(DOCS, SAME, EDITED, trusted=False, rules_from="pr")
    assert source == DocsSource(("README.md",), "base", "author")
    assert read_commit(source, BASE_SHA, HEAD_SHA) == BASE_SHA


@pytest.mark.parametrize("trusted", [True, False])
def test_setting_base_reads_base_for_everyone(trusted: bool) -> None:
    source = source_for(DOCS, SAME, EDITED, trusted=trusted, rules_from="base")
    assert source == DocsSource(("README.md",), "base", "setting")
    assert read_commit(source, BASE_SHA, HEAD_SHA) == BASE_SHA


def test_added_removed_and_mode_changed_documents_count_as_changed() -> None:
    only_readme = _entries(_entry("README.md"))
    assert changed_docs(DOCS, only_readme, SAME) == ("CONTRIBUTING.md",)  # 새로 더함
    assert changed_docs(DOCS, SAME, only_readme) == ("CONTRIBUTING.md",)  # 지움
    linked = _entries(_entry("README.md", mode="120000"), _entry("CONTRIBUTING.md"))
    assert changed_docs(DOCS, SAME, linked) == ("README.md",)
    assert changed_docs(DOCS, {}, {}) == ()
    both = _entries(_entry("README.md", sha="2" * 40), _entry("CONTRIBUTING.md", sha="3" * 40))
    assert changed_docs(DOCS, SAME, both) == DOCS  # 설정의 순서


def test_documents_outside_the_configured_list_are_not_looked_at() -> None:
    head = _entries(_entry("README.md"), _entry("CONTRIBUTING.md"), _entry("docs/x.md"))
    assert source_for(DOCS, SAME, head, trusted=True, rules_from="pr") is None


def test_compare_lists_rules_on_one_side_only() -> None:
    kept, gone, new = _rule("함수 이름은 동사로 시작한다."), _rule("가"), _rule("나")
    removed, added = compare([kept, gone], [new, kept])
    assert removed == (RuleChange(gone.id, "가"),)
    assert added == (RuleChange(new.id, "나"),)
    assert compare([kept], [kept]) == ((), ())


def test_reworded_rule_is_one_removed_and_one_added() -> None:
    old, new = _rule("`print`를 쓰지 않는다."), _rule("`print`를 쓰지 않는다. 로거를 쓴다.")
    source = with_comparison(DocsSource(("README.md",), "pr"), [old], [new])
    assert source.removed == (RuleChange(old.id, old.text),)
    assert source.added == (RuleChange(new.id, new.text),)
