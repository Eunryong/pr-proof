"""models/access.py: PR 작성자의 신뢰 판단 (DESIGN 8.6의 임시 가드레일)."""

import pytest

from app.models.access import may_run


@pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_people_who_can_write_may_run(association: str) -> None:
    assert may_run(association)


@pytest.mark.parametrize(
    "association",
    ["CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER", "NONE", "MANNEQUIN",
     "SOMETHING_NEW", "owner", " OWNER", "OWNER\n", "", None],
)  # fmt: skip
def test_everyone_else_including_unknown_and_empty_may_not(association: str | None) -> None:
    assert not may_run(association)
