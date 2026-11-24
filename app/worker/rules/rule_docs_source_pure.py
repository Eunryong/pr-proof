"""규칙 문서의 원천 (docs/DESIGN.md 4.3): PR이 규칙 문서를 고쳤을 때 어느 커밋의 문서로 규칙을
읽는지 정하고, 그 PR로 없어지는 규칙과 새로 생기는 규칙을 가린다. 외부 호출 없음.

고쳤는지는 두 커밋의 (모드, blob 해시)만 보고 정한다. 모델을 부르지 않는다. 읽을 문서의 목록
(`rules.docs`)과 `rules.from`, 믿는 작성자의 기준은 base의 `.pipeline.yaml`에서만 온다.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Literal

from app.worker.config.repo_config_pure import RulesFrom
from app.worker.core.rule_pure import Rule
from app.worker.git.git_pure import TreeEntry

# base로 읽은 까닭: 작성자가 믿는 작성자가 아님, 설정이 `rules.from: base`
BaseReason = Literal["author", "setting"]


@dataclass(frozen=True)
class RuleChange:
    """PR이 규칙 문서를 고쳐 없어지거나 새로 생기는 규칙. text는 문서의 원문이다(믿지 않는 글)."""

    id: str
    text: str


@dataclass(frozen=True)
class DocsSource:
    """규칙 문서를 고친 PR에서 규칙을 어느 쪽 문서로 읽었는지. 고치지 않은 PR에는 이 값이 없다."""

    changed: tuple[str, ...]  # 고친 규칙 문서의 경로 (`rules.docs`의 순서)
    read_from: RulesFrom
    base_reason: BaseReason | None = None  # read_from이 base일 때만
    # PR의 문서로 읽었을 때만 견준다. 규칙 id로 견주므로 문장이 바뀐 규칙은 양쪽에 하나씩 든다
    removed: tuple[RuleChange, ...] = ()  # base의 문서에만 있는 규칙
    added: tuple[RuleChange, ...] = ()  # PR의 문서에만 있는 규칙
    compare_skipped: str = ""  # base의 규칙을 얻지 못해 견주지 못했으면 그 까닭


def changed_docs(
    docs: Sequence[str], base: Mapping[str, TreeEntry], head: Mapping[str, TreeEntry]
) -> tuple[str, ...]:
    """docs 가운데 두 커밋에서 (모드, blob 해시)가 다른 경로. 한쪽에만 있는 문서도 든다."""

    def shape(entries: Mapping[str, TreeEntry], path: str) -> tuple[str, str] | None:
        entry = entries.get(path)
        return (entry.mode, entry.sha) if entry is not None else None

    return tuple(p for p in dict.fromkeys(docs) if shape(base, p) != shape(head, p))


def source_for(
    docs: Sequence[str],
    base: Mapping[str, TreeEntry],
    head: Mapping[str, TreeEntry],
    *,
    trusted: bool,
    rules_from: RulesFrom,
) -> DocsSource | None:
    """PR이 규칙 문서를 고치지 않았으면 None(base로 읽는다). 고쳤으면 어느 쪽으로 읽을지.

    base와 head는 두 커밋에서 docs의 경로를 찾은 결과다. trusted는 PR 작성자가 믿는 작성자인지다
    (`access_pure.is_trusted`). PR의 문서로 읽는 것은 믿는 작성자의 PR이고 설정이 pr일 때뿐이다.
    """
    changed = changed_docs(docs, base, head)
    if not changed:
        return None
    if rules_from == "base":
        return DocsSource(changed, "base", "setting")
    if not trusted:
        return DocsSource(changed, "base", "author")
    return DocsSource(changed, "pr")


def read_commit(source: DocsSource | None, base_sha: str, head_sha: str) -> str:
    """규칙 문서를 읽는 커밋."""
    return head_sha if source is not None and source.read_from == "pr" else base_sha


def compare(
    base_rules: Sequence[Rule], head_rules: Sequence[Rule]
) -> tuple[tuple[RuleChange, ...], tuple[RuleChange, ...]]:
    """(없어지는 규칙, 새로 생기는 규칙). 규칙 id가 한쪽에만 있는 것이고, 각 묶음의 순서를 따른다.

    규칙 id는 문서 경로와 원문으로 만들므로 문장이 조금이라도 바뀐 규칙은 다른 규칙이다. 짝짓지
    않고 없어짐 하나와 새로 생김 하나로 낸다.
    """
    base_ids = {r.id for r in base_rules}
    head_ids = {r.id for r in head_rules}
    removed = tuple(RuleChange(r.id, r.text) for r in base_rules if r.id not in head_ids)
    added = tuple(RuleChange(r.id, r.text) for r in head_rules if r.id not in base_ids)
    return removed, added


def with_comparison(
    source: DocsSource, base_rules: Sequence[Rule], head_rules: Sequence[Rule]
) -> DocsSource:
    removed, added = compare(base_rules, head_rules)
    return replace(source, removed=removed, added=added)
