import subprocess
from pathlib import Path
from typing import Final

import pytest

from app.models.diff import (
    ChangeBlock,
    added_lines,
    change_blocks,
    hunk_around,
    new_files,
    touches,
)

MODIFY: Final = """\
diff --git a/orders/pricing.py b/orders/pricing.py
index 1111111..2222222 100644
--- a/orders/pricing.py
+++ b/orders/pricing.py
@@ -1,4 +1,5 @@
 import math
-X = 1
+X = 2
+Y = 3

 def f():
@@ -10,2 +11,3 @@ def g():
     a = 1
+    b = 2
     return a
"""


def test_modified_and_added_lines() -> None:
    assert added_lines(MODIFY) == {"orders/pricing.py": frozenset({2, 3, 12})}


def test_new_file() -> None:
    diff = """\
diff --git a/orders/new.py b/orders/new.py
new file mode 100644
--- /dev/null
+++ b/orders/new.py
@@ -0,0 +1,2 @@
+import os
+x = 1
"""
    assert added_lines(diff) == {"orders/new.py": frozenset({1, 2})}


def test_deleted_file_is_skipped() -> None:
    diff = """\
diff --git a/orders/old.py b/orders/old.py
deleted file mode 100644
--- a/orders/old.py
+++ /dev/null
@@ -1,2 +0,0 @@
-import os
-x = 1
"""
    assert added_lines(diff) == {}


def test_added_line_that_looks_like_header() -> None:
    """추가된 내용이 `++ b/x`로 시작하면 diff에는 `+++ b/x`로 보인다. 파일 헤더가 아니다."""
    diff = """\
diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,1 +1,3 @@
 x = 1
+++ b/other.py
+y = 2
"""
    assert added_lines(diff) == {"a.py": frozenset({2, 3})}


def test_single_line_hunk_without_counts() -> None:
    diff = """\
diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1 +1 @@
-x = 1
+x = 2
"""
    assert added_lines(diff) == {"a.py": frozenset({1})}


def test_no_newline_marker() -> None:
    diff = """\
diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1 +1,2 @@
-x = 1
\\ No newline at end of file
+x = 1
+y = 2
"""
    assert added_lines(diff) == {"a.py": frozenset({1, 2})}


def test_rename_uses_new_path() -> None:
    diff = """\
diff --git a/old.py b/new.py
similarity index 90%
rename from old.py
rename to new.py
--- a/old.py
+++ b/new.py
@@ -1,2 +1,2 @@
 x = 1
-y = 1
+y = 2
"""
    assert added_lines(diff) == {"new.py": frozenset({2})}


def test_multiple_files() -> None:
    diff = (
        MODIFY
        + """\
diff --git a/b.py b/b.py
--- a/b.py
+++ b/b.py
@@ -1 +1 @@
-a
+b
"""
    )
    assert set(added_lines(diff)) == {"orders/pricing.py", "b.py"}


def test_touches_range() -> None:
    changed = {"a.py": frozenset({5})}
    assert touches(changed, "a.py", 5, 5)
    assert touches(changed, "a.py", 3, 7)  # 여러 줄 위반의 한 줄만 바뀜
    assert not touches(changed, "a.py", 6, 9)
    assert not touches(changed, "a.py", 4, 4)
    assert not touches(changed, "b.py", 5, 5)


def test_touches_end_before_start_uses_start() -> None:
    assert touches({"a.py": frozenset({5})}, "a.py", 5, 1)


def _one_line(header: str) -> str:
    return f"diff --git x y\n--- /dev/null\n{header}\n@@ -0,0 +1 @@\n+import os\n"


@pytest.mark.parametrize(
    ("header", "path"),
    [
        ('+++ "b/\\353\\252\\250\\353\\223\\210.py"', "모듈.py"),  # core.quotePath=true
        ("+++ b/모듈.py", "모듈.py"),  # core.quotePath=false
        ("+++ b/my file.py\t", "my file.py"),  # 공백이 든 경로 뒤의 탭
        ('+++ "b/say \\"hi\\".py"', 'say "hi".py'),
    ],
    ids=["quoted-utf8", "unquoted-utf8", "space-tab", "escaped-quote"],
)
def test_special_paths_are_read(header: str, path: str) -> None:
    assert added_lines(_one_line(header)) == {path: frozenset({1})}


def test_unknown_prefix_is_skipped_not_misnamed() -> None:
    assert added_lines(_one_line("+++ w/orders/a.py")) == {}


def test_new_files_are_added_and_renamed_paths_only() -> None:
    diff = """\
diff --git a/tests/pricing_test.py b/tests/pricing_test.py
new file mode 100644
index 0000000..1111111
--- /dev/null
+++ b/tests/pricing_test.py
@@ -0,0 +1,2 @@
+new file mode 100644
++++ b/not/a/header.py
diff --git a/orders/empty.py b/orders/empty.py
new file mode 100644
index 0000000..e69de29
diff --git a/orders/old.py b/orders/moved.py
similarity index 100%
rename from orders/old.py
rename to orders/moved.py
diff --git a/orders/a.py b/orders/a.py
index 1111111..2222222 100644
--- a/orders/a.py
+++ b/orders/a.py
@@ -1 +1 @@
-x = 1
+x = 2
diff --git a/orders/gone.py b/orders/gone.py
deleted file mode 100644
--- a/orders/gone.py
+++ /dev/null
@@ -1 +0,0 @@
-x = 1
"""
    assert new_files(diff) == {"tests/pricing_test.py", "orders/empty.py", "orders/moved.py"}


def test_hunk_around_returns_the_hunk_with_that_line() -> None:
    assert hunk_around(MODIFY, "orders/pricing.py", 12, radius=5) == (
        "@@ -10,2 +11,3 @@ def g():\n     a = 1\n+    b = 2\n     return a"
    )
    first = hunk_around(MODIFY, "orders/pricing.py", 3, radius=5)
    assert first.startswith("@@ -1,4 +1,5 @@\n import math\n-X = 1\n+X = 2\n+Y = 3")


def test_hunk_around_cuts_a_long_hunk_to_the_window() -> None:
    body = "".join(f"+line {n}\n" for n in range(1, 101))
    diff = f"diff --git a/n.py b/n.py\n--- /dev/null\n+++ b/n.py\n@@ -0,0 +1,100 @@\n{body}"
    assert hunk_around(diff, "n.py", 50, radius=2) == (
        "@@ -0,0 +1,100 @@\n+line 48\n+line 49\n+line 50\n+line 51\n+line 52"
    )
    assert hunk_around(diff, "n.py", 1, radius=1) == "@@ -0,0 +1,100 @@\n+line 1\n+line 2"


@pytest.mark.parametrize(
    ("file", "line"),
    [("orders/pricing.py", 0), ("orders/pricing.py", 30), ("other.py", 2)],
    ids=["file-unit", "line-not-in-any-hunk", "other-file"],
)
def test_hunk_around_without_a_matching_hunk_is_empty(file: str, line: int) -> None:
    assert hunk_around(MODIFY, file, line, radius=3) == ""


def _git_diff(tmp_path: Path, before: str, after: str, *extra: str) -> str:
    """실제 git이 낸 diff (문맥 줄 기본값, 또는 extra)."""
    (tmp_path / "a.py").write_text(before)
    (tmp_path / "b.py").write_text(after)
    proc = subprocess.run(
        ["git", "diff", "--no-index", "--no-color", *extra, "a.py", "b.py"],
        cwd=tmp_path, capture_output=True, text=True,
    )  # fmt: skip
    return proc.stdout.replace("a/b.py", "a/a.py").replace("b/b.py", "b/a.py")


PR_PRICING: Final = "".join(f"L{n}\n" for n in range(1, 13))


@pytest.mark.parametrize("context", [(), ("-U0",)], ids=["context-3", "no-context"])
def test_change_blocks_split_at_unchanged_lines(tmp_path: Path, context: tuple[str, ...]) -> None:
    lines = PR_PRICING.splitlines(keepends=True)
    after = lines[:5] + lines[6:9] + lines[10:]  # L6, L10을 지움
    after[0] = "L1 changed\n"
    after.insert(3, "new\n")  # L3 뒤에 더함
    diff = _git_diff(tmp_path, PR_PRICING, "".join(after), *context)
    assert change_blocks(diff) == (
        ChangeBlock("a.py", frozenset({1}), frozenset({1})),
        ChangeBlock("a.py", frozenset({4}), frozenset()),  # 더하기만 함
        ChangeBlock("a.py", frozenset({6, 7}), frozenset({6})),  # L6을 지운 자리: L5(6), L7(7)
        ChangeBlock("a.py", frozenset({9, 10}), frozenset({10})),  # L10을 지운 자리
    )


def test_change_block_keeps_the_old_side_lines_it_replaced(tmp_path: Path) -> None:
    """수정 diff의 앞쪽은 PR head라, 덩어리가 바꾼 위반의 줄을 old_lines로 맞춰 본다."""
    after = "L1\nnew\nL4\nL5\nL6\nextra\nL7\n"  # L2–L3을 한 줄로, L6 뒤에 한 줄 더함
    diff = _git_diff(tmp_path, "L1\nL2\nL3\nL4\nL5\nL6\nL7\n", after, "-U1")
    blocks = change_blocks(diff)
    assert blocks == (
        ChangeBlock("a.py", frozenset({2}), frozenset({2, 3})),
        ChangeBlock("a.py", frozenset({6}), frozenset()),
    )
    assert [b.review_line for b in blocks] == [2, 6]


def test_change_block_deleting_the_first_or_last_line(tmp_path: Path) -> None:
    first = _git_diff(tmp_path, "a\nb\nc\n", "b\nc\n", "-U0")
    assert change_blocks(first) == (ChangeBlock("a.py", frozenset({1}), frozenset({1})),)
    last = _git_diff(tmp_path, "a\nb\nc\n", "a\nb\n")
    (block,) = change_blocks(last)
    assert block == ChangeBlock("a.py", frozenset({2, 3}), frozenset({3}))
    assert block.review_line == 2  # 지운 자리의 앞줄. 고친 파일에 있는 줄이다


def test_change_blocks_skip_deleted_files() -> None:
    diff = (
        "diff --git a/x.py b/x.py\ndeleted file mode 100644\n--- a/x.py\n+++ /dev/null\n"
        "@@ -1 +0,0 @@\n-x = 1\n"
    )
    assert change_blocks(diff) == ()
