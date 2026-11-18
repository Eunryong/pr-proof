"""models/rejection.py: 사람이 닫은 제안의 위반을 줄 내용으로 기억하고 맞춰 본다."""

from typing import Final

from app.models.finding import Finding
from app.models.rejection import finding_key, fixed_keys, mark_rejected, with_line_text

F: Final = Finding("a.py", 2, "error", "unused", "lint", "x", "F401", True)
LINES: Final = {"a.py": ["x = 1", "    import sys  ", "", "y"]}


def _lines(path: str):
    return LINES.get(path)


def test_line_text_is_stripped_and_missing_lines_are_none() -> None:
    past_end = Finding("a.py", 9, "error", "m", "lint", "x", "E1")
    no_file = Finding("gone.py", 1, "error", "m", "lint", "x", "E1")
    out = with_line_text([F, past_end, no_file], _lines)
    assert [f.line_text for f in out] == ["import sys", None, None]


def test_blank_line_has_no_key() -> None:
    (blank,) = with_line_text([Finding("a.py", 3, "error", "m", "lint", "x", "W391")], _lines)
    assert blank.line_text == "" and finding_key(blank) is None


def test_mark_rejected_matches_file_code_and_content_not_line_number() -> None:
    (moved,) = with_line_text([Finding("a.py", 2, "error", "unused", "lint", "x", "F401")], _lines)
    rejected = {("a.py", "F401", "import sys"): 900}
    assert mark_rejected([moved], rejected)[0].rejected_in == 900
    other_code = Finding("a.py", 2, "error", "m", "lint", "x", "E401", line_text="import sys")
    assert mark_rejected([other_code], rejected)[0].rejected_in is None


def test_fixed_keys_skip_remaining_and_already_rejected() -> None:
    fixed = Finding("a.py", 2, "error", "unused", "lint", "x", "F401", line_text="import sys")
    left = Finding("a.py", 4, "error", "ambiguous", "lint", "x", "E741", line_text="y")
    old = Finding("a.py", 1, "error", "m", "lint", "x", "F811", line_text="x = 1", rejected_in=900)
    remaining = [Finding("a.py", 3, "error", "ambiguous", "lint", "x", "E741")]  # 줄이 밀림
    assert fixed_keys([fixed, left, old], remaining) == [("a.py", "F401", "import sys")]
