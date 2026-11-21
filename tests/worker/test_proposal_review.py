"""수정안 PR의 줄 리뷰 코멘트 묶기 (DESIGN.md 4.9)."""

from types import SimpleNamespace
from typing import Final

from app.models.model_fix import Change, Explanation
from app.worker import proposal

NO_RULES: Final = SimpleNamespace(outcomes=(), violations=())


def _fix(*changes: Change) -> SimpleNamespace:
    return SimpleNamespace(explanation=Explanation("s", changes))


def test_one_comment_per_file_and_rule_on_the_first_fixed_line() -> None:
    """같은 (파일, 규칙)은 고친 곳이 여럿이어도 첫 줄에 코멘트 하나다. 나머지 줄은 그 글에 있다."""
    fix = _fix(
        Change("a.py", 30, "R1", "상수로 바꿈"),
        Change("a.py", 7, "R1", "상수로 바꿈"),
        Change("a.py", 12, "R1", "상수로 바꿈"),
        Change("a.py", 12, "R2", "로거로 바꿈"),
        Change("b.py", 3, "R1", "b의 이유"),
    )
    comments = proposal.review_comments(fix, NO_RULES)
    assert [(c.path, c.line) for c in comments] == [("a.py", 7), ("a.py", 12), ("b.py", 3)]
    first, second, third = comments
    assert "`상수로 바꿈`" in first.body
    assert first.body.endswith("이 파일에서 같은 규칙으로 고친 다른 줄: 12, 30")
    assert "`로거로 바꿈`" in second.body and "다른 줄" not in second.body
    assert "`b의 이유`" in third.body and "다른 줄" not in third.body


def test_other_lines_are_cut_at_the_limit() -> None:
    count = proposal.MAX_REVIEW_OTHER_LINES + 6
    fix = _fix(*(Change("a.py", n, "R1", "w") for n in range(1, count + 1)))
    (comment,) = proposal.review_comments(fix, NO_RULES)
    assert comment.line == 1
    assert comment.body.endswith(f"{proposal.MAX_REVIEW_OTHER_LINES + 1} 외 5곳")


def test_no_explanation_means_no_comments() -> None:
    assert proposal.review_comments(SimpleNamespace(explanation=None), NO_RULES) == []


def test_explanation_text_groups_repeated_reasons_and_keeps_every_reason() -> None:
    """본문의 "모델의 설명"은 같은 (파일, 규칙, 이유)를 한 줄로 묶는다. 다른 이유는 남는다."""
    text = proposal.explanation_text(
        Explanation(
            "요약",
            (
                Change("a.py", 30, "R1", "상수로 바꿈"),
                Change("a.py", 7, "R1", "상수로 바꿈"),
                Change("a.py", 9, "R1", "옛 답의 다른 이유"),
                Change("b.py", 3, "R1", "상수로 바꿈"),
            ),
        )
    )
    assert text.splitlines() == [
        "요약",
        "",
        "- a.py R1: 상수로 바꿈 (줄 7, 30)",
        "- a.py R1: 옛 답의 다른 이유 (줄 9)",
        "- b.py R1: 상수로 바꿈 (줄 3)",
    ]
    assert proposal.explanation_text(None) == ""
