from datetime import UTC, datetime
from typing import Final

from app.shared.db_pure import Record, SeenViolation
from app.worker.core.diff_pure import hunk_removing
from app.worker.core.violation_pure import Violation
from app.worker.store.records_pure import (
    Target,
    author_fixed,
    author_records,
    context_around,
    failure_records,
    fix_pr_records,
    mark_rejected,
    rejected_lines,
    targets_from_json,
    targets_to_json,
    text_of,
)

PATCH: Final = (
    "diff --git a/orders/a.py b/orders/a.py\n--- a/orders/a.py\n+++ b/orders/a.py\n"
    "@@ -1,2 +1,2 @@\n x = 1\n-print(x)\n+log(x)\n"
    "@@ -9 +9 @@\n-print(y)\n+log(y)\n"
)
TARGET: Final = Target("v1", "R", "print을 쓰지 않는다", "orders/a.py", 9, "print(y)", "ctx")


def test_hunk_removing_picks_the_hunk_that_removes_the_line() -> None:
    last = "@@ -9 +9 @@\n-print(y)\n+log(y)"
    assert hunk_removing(PATCH, "orders/a.py", "  print(y) ", 60) == last
    # 그 줄을 지운 hunk가 없으면 그 파일의 첫 hunk, 파일이 없으면 ""
    assert hunk_removing(PATCH, "orders/a.py", "z", 60).startswith("@@ -1,2 +1,2 @@")
    assert hunk_removing(PATCH, "other.py", "print(y)", 60) == ""
    assert hunk_removing(PATCH, "orders/a.py", "print(x)", 2) == "@@ -1,2 +1,2 @@\n x = 1"


def test_context_around_keeps_five_lines_each_side_and_stays_inside_the_file() -> None:
    lines = [f"l{i}" for i in range(1, 21)]
    assert context_around(lines, 10).split("\n") == [f"l{i}" for i in range(5, 16)]
    assert context_around(lines, 1).split("\n") == [f"l{i}" for i in range(1, 7)]
    assert context_around(lines, 0) == "" and context_around(lines, 21) == ""


def test_fix_pr_and_failure_records_carry_the_target_and_its_hunk() -> None:
    assert targets_from_json(targets_to_json([TARGET])) == (TARGET,)
    (rejected,) = fix_pr_records(
        "rejected", repo="o/r", pr_number=1, fix_pr_number=900, commit_sha="h",
        targets=[TARGET], patch=PATCH,
    )  # fmt: skip
    assert (rejected.kind, rejected.origin, rejected.source_id) == (
        "rejected", "fix_pr", "fix_pr:900:v1",
    )  # fmt: skip
    assert rejected.diff == "@@ -9 +9 @@\n-print(y)\n+log(y)" and rejected.context == "ctx"
    (failed,) = failure_records(
        repo="o/r", pr_number=1, head_sha="h", attempt_id=4, targets=[TARGET], patch=PATCH,
        detail="x" * 5000,
    )  # fmt: skip
    assert (failed.kind, failed.source_id, len(failed.detail)) == ("failed", "attempt:4:v1", 2000)
    assert "print을 쓰지 않는다" in text_of(failed) and "-print(y)" in text_of(failed)


def _seen(vid: str, line_text: str | None = "print(x)", **kw) -> SeenViolation:
    fields = {"repo": "o/r", "pr_number": 1, "head_sha": "old", "violation_id": vid,
              "rule_id": "R", "file": "orders/a.py", "line": 2, "line_text": line_text,
              "message": "m", "unit": "line", "context": "x = 1\nprint(x)",
              "created_at": datetime.now(UTC)} | kw  # fmt: skip
    return SeenViolation(**fields)


def test_author_fixed_only_when_the_line_is_gone_and_the_rule_was_checked() -> None:
    files = {"orders/a.py": ["x = 1", "log(x)", "  print(z)"]}
    previous = [
        _seen("fixed"),
        _seen("still", line_text="print(z)"),  # 줄이 그대로 남았다
        _seen("found_again", line_text="print(q)"),  # 이번 검사에도 있다
        _seen("unchecked", rule_id="R2"),  # 이번에 그 규칙을 검사하지 못했다
        _seen("deleted", file="gone.py"),  # 파일을 지웠다
        _seen("path", line_text=None, unit="file"),  # 파일 단위 위반
    ]
    fixed = author_fixed(previous, frozenset({"found_again"}), frozenset({"R"}), files.get)
    assert [v.violation_id for v in fixed] == ["fixed"]
    diff = "--- a/orders/a.py\n+++ b/orders/a.py\n@@ -2 +2 @@\n-print(x)\n+log(x)\n"
    (record,) = author_records(fixed, rule_texts={"R": "규칙"}, head_sha="new",
                               diff_of=lambda f: diff)  # fmt: skip
    assert (record.kind, record.origin, record.source_id, record.commit_sha) == (
        "adopted", "author_commit", "seen:old:fixed", "new",
    )  # fmt: skip
    assert record.rule_text == "규칙" and record.diff == "@@ -2 +2 @@\n-print(x)\n+log(x)"


def _stored(id: int, kind: str, number: int, line_text: str = "print(x)") -> Record:
    return Record(id=id, repo="o/r", kind=kind, origin="fix_pr", source_id=f"s{id}", rule_id="R",
                  rule_text="t", file="a.py", line_text=line_text, context="", diff="",
                  fix_pr_number=number, created_at=datetime.now(UTC))  # fmt: skip


def test_rejected_lines_use_only_rejections_and_the_latest_number() -> None:
    table = rejected_lines([_stored(2, "rejected", 905), _stored(1, "rejected", 900),
                            _stored(3, "adopted", 907, "log(x)")])  # fmt: skip
    assert table == {("a.py", "R", "print(x)"): 905}


def test_mark_rejected_needs_the_same_file_rule_and_line_text() -> None:
    def v(vid: str, line_text: str | None, file: str = "a.py", rule: str = "R") -> Violation:
        return Violation(id=vid, rule_id=rule, file=file, line=2, message="m",
                         found_by="checker", line_text=line_text)  # fmt: skip

    table = {("a.py", "R", "print(x)"): 900}
    marked = mark_rejected(
        [v("same", " print(x) "), v("edited", "print(x + 1)"), v("other_rule", "print(x)",
         rule="R2"), v("other_file", "print(x)", file="b.py"), v("empty", None)],
        table,
    )  # fmt: skip
    assert [m.rejected_in for m in marked] == [900, None, None, None, None]
