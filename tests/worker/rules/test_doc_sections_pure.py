from datetime import UTC, datetime
from typing import Final

from app.worker.core.rule_pure import CheckMethod, Rule, RuleSet, RuleSource
from app.worker.rule_reading_pure import Document, sections
from app.worker.rules.doc_sections_pure import (
    always_read,
    chunk_of,
    chunks,
    excerpt,
    pick,
    query_text,
    scoped_to,
    section_key,
)

DOC: Final = Document(
    "README.md", "b" * 40,
    "# 안내서\n\n머리 글\n\n## 소개\n\n소개 글\n\n## 코드 규칙\n\n- 규칙 하나\n\n"
    "## 주문\n\n주문 글\n\n### 결제\n\n```\n# 코드 블록의 샵은 제목이 아니다\n```\n결제 규칙\n",
)  # fmt: skip
NOW: Final = datetime(2026, 11, 20, tzinfo=UTC)


def _by_heading() -> dict[tuple[str, ...], object]:
    return {c.heading: c for c in chunks(DOC)}


def test_chunks_cover_every_line_once_and_keep_ancestor_headings() -> None:
    got = chunks(DOC)
    assert [c.heading for c in got] == [
        (),
        ("소개",),
        ("코드 규칙",),
        ("주문",),
        ("주문", "결제"),
    ]
    lines = DOC.text.splitlines()
    covered = sorted(i for c in got for i in range(c.start, c.end))
    assert covered == list(range(len(lines)))
    pay = _by_heading()[("주문", "결제")]
    assert [lines[i] for i in pay.keep] == ["# 안내서", "## 주문"]
    assert "# 코드 블록의 샵은 제목이 아니다" in pay.text
    assert pay.label == "README.md > 주문 > 결제" and chunks(DOC)[0].label == "README.md (머리)"


def test_chunk_id_changes_only_with_its_own_text() -> None:
    edited = Document(DOC.path, "c" * 40, DOC.text.replace("소개 글", "새 소개 글"))
    before, after = _by_heading(), {c.heading: c for c in chunks(edited)}
    assert before[("소개",)].id != after[("소개",)].id
    assert before[("주문", "결제")].id == after[("주문", "결제")].id


def test_always_read_is_the_head_and_rule_like_titles() -> None:
    by = _by_heading()
    assert always_read(by[()]) and always_read(by[("코드 규칙",)])
    assert not always_read(by[("소개",)]) and not always_read(by[("주문", "결제")])
    doc = Document(
        "C.md", "x", "## Contributing\n\n글\n\n## 스타일 가이드\n\n글\n\n## Style\n\n글\n"
    )
    assert [always_read(c) for c in chunks(doc)] == [True, True, False]


def test_excerpt_keeps_the_section_paths_of_the_whole_document() -> None:
    pay = _by_heading()[("주문", "결제")]
    part = excerpt(DOC, [pay])
    assert "소개 글" not in part.text and "주문 글" not in part.text
    paths = {s.path for s in sections(part.text)[1]}
    assert ("주문", "결제") in paths
    assert sections(part.text)[0] == "안내서"
    assert part.path == DOC.path and part.blob_sha == DOC.blob_sha


def test_pick_orders_always_scoped_then_hits_by_rank_and_stops_at_the_byte_cap() -> None:
    by = _by_heading()
    all_chunks = chunks(DOC)
    hits = [(by[("소개",)].id, 0.95), (by[("주문", "결제")].id, 0.85), ("gone", 0.99)]
    got = pick(all_chunks, scoped={by[("주문",)].id}, hits=hits)
    # 검색이 찾은 절은 유사도와 관계없이 넣는다 (없어진 조각 id는 버린다)
    assert [c.heading for c in got] == [(), ("소개",), ("코드 규칙",), ("주문",), ("주문", "결제")]
    small = pick(all_chunks, scoped=set(), hits=hits,
                 max_bytes=by[()].size + by[("코드 규칙",)].size)  # fmt: skip
    assert [c.heading for c in small] == [(), ("코드 규칙",)]


def _rule(section: str, scope: tuple[str, ...] = ()) -> Rule:
    source = RuleSource("README.md", section, "b")
    return Rule(id="R-1", text="규칙 하나", source=source, origin="document", scope=scope,
                check=CheckMethod("checker"), fix="model")  # fmt: skip


def test_scoped_to_uses_saved_rules_with_a_stated_scope() -> None:
    def rs(*rules: Rule) -> RuleSet:
        return RuleSet(rules=rules, sources=(), pipeline_version="3", prompt_version="p", model="m")

    saved = {"a": rs(_rule("주문", ("orders/**",))), "b": rs(_rule("소개")), "c": rs()}
    assert scoped_to(saved, ["orders/x.py"]) == {"a"}
    assert scoped_to(saved, ["docs/y.md"]) == set()


def test_chunk_of_matches_the_located_section() -> None:
    all_chunks = chunks(DOC)
    assert chunk_of(_rule("주문 > 결제"), all_chunks).heading == ("주문", "결제")
    assert chunk_of(_rule(""), all_chunks).heading == ()
    assert chunk_of(_rule("없는 절"), all_chunks) is None


def test_query_has_paths_folders_and_added_code_and_key_follows_the_chunk() -> None:
    q = query_text(["orders/services/pay.py", "README.md"], "if x > 3:")
    assert "orders/services/pay.py" in q and "폴더: orders/services" in q and "if x > 3:" in q
    a, b = chunks(DOC)[1], chunks(DOC)[2]
    assert section_key(a, "haiku") != section_key(b, "haiku")
    assert section_key(a, "haiku") != section_key(a, "sonnet")
