"""큰 변경을 묶음으로 나누기 (docs/DESIGN.md 4.5 끝 "큰 변경"): 나누는 규칙, 요약의 답, 요약을
모은 블록."""

import pytest

from app.worker.rules.diff_batches_pure import (
    CONTINUED,
    MAX_CHANGE_CHARS,
    MAX_CHANGES,
    Batch,
    Split,
    batch_blocks,
    changes_blocks,
    changes_from_json,
    changes_to_json,
    digest_key,
    digest_prompt,
    other_paths,
    parse_changes,
    split,
    unseen_paths,
)
from app.worker.rules.pr_description_pure import parse_diff, render_view


def _new_file(name: str, lines: int, width: int = 40) -> str:
    body = "".join(f"+{n:05d}{'x' * width}\n" for n in range(1, lines + 1))
    return (
        f"diff --git a/{name} b/{name}\nnew file mode 100644\n--- /dev/null\n+++ b/{name}\n"
        f"@@ -0,0 +1,{lines} @@\n{body}"
    )


def _rows(parts: Split, path: str) -> list[str]:
    """path의 줄 번호가 묶음들에 나온 순서."""
    out = []
    for batch in parts.batches:
        inside = False
        for line in batch.text.splitlines():
            if line.startswith("파일: "):
                inside = line.split()[1] == path
            elif inside and " + " in line:
                out.append(line.split(" + ", 1)[1][:5])
    return out


def test_diff_that_fits_is_one_batch_with_the_same_text_as_one_call() -> None:
    files = parse_diff(_new_file("a.py", 5) + _new_file("b.py", 5))
    parts = split(files)
    assert parts.single and parts.left == () and parts.files == 2
    assert parts.batches == (Batch(render_view(files).text, ("a.py", "b.py")),)


def test_files_go_whole_into_batches_in_order() -> None:
    files = parse_diff("".join(_new_file(f"f{n}.py", 20) for n in range(6)))
    one = len(render_view(files[:1]).text)
    parts = split(files, limit=one * 2 + 10)
    assert [b.paths for b in parts.batches] == [
        ("f0.py", "f1.py"), ("f2.py", "f3.py"), ("f4.py", "f5.py"),
    ]  # fmt: skip
    assert not parts.single and parts.left == ()
    assert all(len(b.text) <= one * 2 + 10 for b in parts.batches)


def test_file_larger_than_a_batch_is_spread_by_line_without_losing_any() -> None:
    files = parse_diff(_new_file("a.py", 3) + _new_file("big.py", 100) + _new_file("z.py", 3))
    parts = split(files, limit=1500)
    assert len(parts.batches) > 2 and parts.left == ()
    assert all(len(b.text) <= 1500 for b in parts.batches)
    assert _rows(parts, "big.py") == [f"{n:05d}" for n in range(1, 101)]
    assert parts.batches[0].paths[:2] == ("a.py", "big.py")
    assert f"파일: big.py (새 파일) ({CONTINUED})" in parts.batches[1].text
    assert CONTINUED not in parts.batches[0].text
    assert parts.batches[-1].paths[-1] == "z.py"


def test_largest_files_are_left_out_first_so_small_files_are_still_seen() -> None:
    """큰 파일 하나가 앞에 있어도 뒤의 작은 파일들이 밀려나지 않는다."""
    diff = _new_file("a_lock.json", 2000) + "".join(_new_file(f"f{n}.py", 10) for n in range(8))
    parts = split(parse_diff(diff), limit=1500, max_batches=4)
    assert parts.left == ("a_lock.json",) and parts.files == 9
    assert len(parts.batches) <= 4
    assert {p for b in parts.batches for p in b.paths} == {f"f{n}.py" for n in range(8)}


def test_batch_count_never_exceeds_the_cap() -> None:
    diff = "".join(_new_file(f"f{n:02d}.py", 10 + n) for n in range(30))
    parts = split(parse_diff(diff), limit=1200, max_batches=3)
    assert len(parts.batches) <= 3 and parts.left
    seen = {p for b in parts.batches for p in b.paths}
    assert seen.isdisjoint(parts.left) and len(seen) + len(parts.left) == 30
    assert "f00.py" in seen and "f29.py" in parts.left  # 큰 것부터 뺀다


def test_line_longer_than_a_batch_is_cut() -> None:
    parts = split(parse_diff(_new_file("min.js", 1, width=5000)), limit=800)
    assert len(parts.batches) == 1 and len(parts.batches[0].text) <= 800


def test_other_files_are_named_for_each_batch() -> None:
    parts = Split((Batch("A", ("a.py", "big.py")), Batch("B", ("big.py", "c.py"))), ("x.bin",), 4)
    assert other_paths(parts, 0) == ("c.py", "x.bin")
    assert other_paths(parts, 1) == ("a.py", "x.bin")
    blocks = "".join(batch_blocks(parts, 0, "t"))
    assert "<<<DIFF tag=t>>>" in blocks and "\nA\n<<<END DIFF tag=t>>>" in blocks
    assert "<<<OTHER_FILES tag=t>>>" in blocks and "\nc.py\nx.bin\n<<<END OTHER_FILES" in blocks
    assert "묶음 2개로 나눈 것 중 2번째" in digest_prompt(parts, 1)
    alone = Split((Batch("A", ("a.py",)),), (), 1)
    assert "OTHER_FILES" not in "".join(batch_blocks(alone, 0, "t"))


def test_changes_are_cut_to_the_limits_and_must_be_strings() -> None:
    long = "가" * (MAX_CHANGE_CHARS + 50)
    got = parse_changes({"changes": ["a.py:  줄\n바꿈 ", "", long, *["x"] * 100]})
    assert got[0] == "a.py: 줄 바꿈" and got[1] == "가" * MAX_CHANGE_CHARS
    assert len(got) == MAX_CHANGES
    assert changes_from_json(changes_to_json(("a", "b"))) == ("a", "b")
    for bad in ({}, {"changes": "글"}, {"changes": [1]}):
        with pytest.raises(ValueError):
            parse_changes(bad)
    with pytest.raises(ValueError):
        changes_from_json("[]")


def test_digest_key_follows_the_batch_text_and_model() -> None:
    a, b = Batch("A", ("a.py",)), Batch("B", ("a.py",))
    assert digest_key(a, "sonnet") == digest_key(Batch("A", ("other.py",)), "sonnet")
    assert len({digest_key(a, "sonnet"), digest_key(b, "sonnet"), digest_key(a, "opus")}) == 3


def test_collected_changes_name_batches_and_what_no_call_saw() -> None:
    parts = Split((Batch("A", ("a.py", "b.py")), Batch("B", ("c.py",)), Batch("C", ("d.py",))),
                  ("x.bin",), 5)  # fmt: skip
    changes = [("a.py: 타임아웃을 5로 줄임",), None, ()]
    assert unseen_paths(parts, changes) == ("x.bin", "c.py")
    text = "".join(changes_blocks(parts, changes, "t"))
    assert "묶음 1 (파일: a.py, b.py)\n- a.py: 타임아웃을 5로 줄임\n" in text
    assert "묶음 2" not in text
    assert "묶음 3 (파일: d.py)\n- (요약 없음)\n<<<END CHANGES tag=t>>>" in text
    assert "<<<NOT_SEEN tag=t>>>" in text and "\nx.bin\nc.py\n<<<END NOT_SEEN tag=t>>>" in text
    done = "".join(changes_blocks(Split(parts.batches, (), 4), [(), (), ()], "t"))
    assert "NOT_SEEN" not in done
