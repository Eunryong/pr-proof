from typing import Final

from app.models.diff import added_lines, touches

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
