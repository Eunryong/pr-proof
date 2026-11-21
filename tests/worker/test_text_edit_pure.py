from app.worker.text_edit_pure import TextEdit, apply_edits


def _e(row, col, end_row, end_col, content="") -> TextEdit:
    return TextEdit(row=row, column=col, end_row=end_row, end_column=end_col, content=content)


def test_delete_whole_line() -> None:
    text = "import os\nimport sys\nx = 1\n"
    assert apply_edits(text, [[_e(1, 1, 2, 1)]]) == ("import sys\nx = 1\n", 1)


def test_replace_inside_line() -> None:
    assert apply_edits("x  = 1\n", [[_e(1, 2, 1, 4, " ")]]) == ("x = 1\n", 1)


def test_multiple_groups_apply_from_the_end() -> None:
    text = "a\nb\nc\n"
    new, count = apply_edits(text, [[_e(1, 1, 2, 1)], [_e(3, 1, 4, 1)]])
    assert (new, count) == ("b\n", 2)


def test_overlapping_group_is_skipped() -> None:
    text = "import os, sys\n"
    # 두 위반이 같은 import 문을 각각 다시 쓴다. 하나만 적용하고 다음 회차로 미룬다
    groups = [[_e(1, 1, 1, 15, "import sys")], [_e(1, 1, 1, 15, "import os")]]
    new, count = apply_edits(text, groups)
    assert (new, count) == ("import sys\n", 1)


def test_group_with_multiple_edits_is_all_or_nothing() -> None:
    text = "aaaa\nbbbb\n"
    groups = [[_e(1, 1, 1, 2, "X")], [_e(1, 3, 1, 4, "Y"), _e(1, 1, 1, 2, "Z")]]
    assert apply_edits(text, groups) == ("Xaaa\nbbbb\n", 1)


def test_end_after_last_line() -> None:
    text = "x = 1\nimport os"
    assert apply_edits(text, [[_e(2, 1, 3, 1)]]) == ("x = 1\n", 1)


def test_columns_are_characters_not_bytes() -> None:
    text = 's = "한글"  ;x = 1\n'
    start = text.index(";") + 1  # 1부터 세는 문자 열
    assert apply_edits(text, [[_e(1, start, 1, start + 1, "\n")]])[0] == 's = "한글"  \nx = 1\n'


def test_crlf_is_kept() -> None:
    text = "import os\r\nx = 1\r\n"
    assert apply_edits(text, [[_e(1, 1, 2, 1)]]) == ("x = 1\r\n", 1)


def test_two_inserts_at_same_point_conflict() -> None:
    assert apply_edits("x\n", [[_e(1, 1, 1, 1, "a")], [_e(1, 1, 1, 1, "b")]]) == ("ax\n", 1)


def test_empty_group_is_ignored() -> None:
    assert apply_edits("x\n", [[]]) == ("x\n", 0)
