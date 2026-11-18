import json

from app.services import github


def test_list_open_prs_parses_fields() -> None:
    calls = []

    def runner(args: list[str]) -> str:
        calls.append(args)
        pr = {"headRefOid": "h1", "baseRefOid": "b1", "headRefName": "f"}
        return json.dumps(
            [pr | {"number": 1, "isDraft": False}, pr | {"number": 2, "isDraft": True}]
        )

    prs = github.list_open_prs("o/r", runner)
    assert prs[0] == github.OpenPR(
        number=1, head_sha="h1", base_sha="b1", head_ref="f", draft=False
    )
    assert prs[1].draft is True
    assert calls[0][:4] == ["pr", "list", "--repo", "o/r"]


def test_no_open_prs() -> None:
    assert github.list_open_prs("o/r", lambda args: "[]") == []
