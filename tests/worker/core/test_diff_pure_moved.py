"""수정이 자리만 옮긴 base의 줄을 PR이 바꾼 줄에서 빼기 (app/worker/core/diff_pure.py, BACKLOG의
I001 항목)."""

from typing import Final

from app.worker.core.diff_pure import added_lines, added_lines_except_moved, added_texts

MOVED_DIFF: Final = """\
diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,3 +1,4 @@
+import abc
+import os
 import sys
-import os
 x = 1
"""
PR_ADDS_ABC: Final = "--- a/a.py\n+++ b/a.py\n@@ -1,0 +1,1 @@\n+import abc\n"


def test_line_only_moved_by_a_fix_is_not_a_changed_line() -> None:
    assert added_lines(MOVED_DIFF) == {"a.py": frozenset({1, 2})}
    assert added_lines_except_moved(MOVED_DIFF, added_texts(PR_ADDS_ABC)) == {
        "a.py": frozenset({1})
    }


def test_pr_own_copy_of_a_base_line_stays_changed() -> None:
    """PR이 base와 같은 내용의 줄을 더했으면(옮긴 것과 같아 보여도) 그 개수만큼 PR의 줄이다."""
    pr_added = added_texts("--- a/a.py\n+++ b/a.py\n@@ -1,0 +1,1 @@\n+import os\n")
    assert added_lines_except_moved(MOVED_DIFF, pr_added) == {"a.py": frozenset({1, 2})}


def test_nothing_removed_means_nothing_moved_and_other_files_keep_their_lines() -> None:
    diff = MOVED_DIFF + "--- /dev/null\n+++ b/b.py\n@@ -0,0 +1,1 @@\n+import os\n"
    assert added_lines_except_moved(diff, {}) == {"a.py": frozenset({1}), "b.py": frozenset({1})}
    assert added_lines_except_moved("", {}) == {}


def test_added_texts_counts_repeated_lines() -> None:
    diff = "--- a/a.py\n+++ b/a.py\n@@ -0,0 +1,2 @@\n+x = 1\n+x = 1\n"
    assert added_texts(diff) == {"a.py": {"x = 1": 2}}
