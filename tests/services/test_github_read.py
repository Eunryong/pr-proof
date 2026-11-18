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


def test_list_comments_since_keeps_multiline_bodies_and_drops_older() -> None:
    from app.services.github import list_comments_since

    lines = [
        json.dumps({"id": 1, "author_association": "MEMBER", "created_at": "2026-11-17T10:01:00Z",
                    "body": "/recheck\nwhy"}),
        json.dumps({"id": 2, "author_association": "OWNER", "created_at": "2026-11-17T09:00:00Z",
                    "body": "/fix lint"}),  # 예전에 달고 최근에 고친 코멘트
    ]  # fmt: skip
    calls = []

    def runner(args):
        calls.append(args)
        return "\n".join(lines) + "\n"

    comments = list_comments_since("o/r", 3, "2026-11-17T10:00:00Z", runner)
    assert [(c.id, c.body) for c in comments] == [(1, "/recheck\nwhy")]
    assert "repos/o/r/issues/3/comments?since=2026-11-17T10:00:00Z" in calls[0]
