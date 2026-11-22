import json

import pytest

from app.api import github


def test_list_open_prs_parses_fields() -> None:
    calls = []

    def runner(args: list[str]) -> str:
        calls.append(args)
        pr = {"headRefOid": "h1", "baseRefOid": "b1", "headRefName": "f"}
        return json.dumps(
            [pr | {"number": 1, "isDraft": False, "author": {"login": "a", "is_bot": False}},
             pr | {"number": 2, "isDraft": True}]
        )  # fmt: skip

    prs = github.list_open_prs("o/r", runner)
    assert prs[0] == github.OpenPR(
        number=1, head_sha="h1", base_sha="b1", head_ref="f", draft=False, author_login="a"
    )
    assert prs[1].author_login == ""
    assert prs[1].draft is True
    assert calls[0][:4] == ["pr", "list", "--repo", "o/r"]


def test_auth_ok_runs_gh_auth_status_and_reads_failure_as_false() -> None:
    calls: list[list[str]] = []

    def ok(args: list[str]) -> str:
        calls.append(args)
        return ""

    def expired(args: list[str]) -> str:
        raise github.GitHubReadError("gh auth status 실패")

    assert github.auth_ok(ok) is True and calls == [["auth", "status"]]
    assert github.auth_ok(expired) is False


def test_no_open_prs() -> None:
    assert github.list_open_prs("o/r", lambda args: "[]") == []


def test_list_comments_since_keeps_multiline_bodies_and_drops_older() -> None:
    from app.api.github import list_comments_since

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


def _feedback_runner(calls: list[list[str]], **by_path: list[dict]):
    """gh의 --jq를 흉내 내지 않고, 경로마다 jq가 낼 줄(JSON 한 줄씩)을 돌려준다."""

    def runner(args: list[str]) -> str:
        calls.append(args)
        path = next(a for a in args if a.startswith("repos/"))
        _, _, _, area, _, what = path.split("/")
        key = f"{area}/{what}"
        return "".join(json.dumps(item) + "\n" for item in by_path.get(key, []))

    return runner


def test_list_feedback_reads_three_places_with_paginate_and_jq() -> None:
    calls: list[list[str]] = []
    runner = _feedback_runner(
        calls,
        **{
            "pulls/comments": [
                {"id": 1, "author_association": "NONE", "body": "줄\n코멘트", "login": "Copilot"}
            ],
            "pulls/reviews": [
                {"id": 2, "author_association": "MEMBER", "body": "리뷰 본문", "login": "kim"}
            ],
            "issues/comments": [
                {"id": 3, "author_association": "OWNER", "body": "일반", "login": None},
            ],
        },
    )
    items = github.list_feedback("o/r", 5, runner)
    assert items == [
        github.FeedbackItem("review_comment", 1, "Copilot", "NONE", "줄\n코멘트"),
        github.FeedbackItem("review", 2, "kim", "MEMBER", "리뷰 본문"),
        github.FeedbackItem("issue_comment", 3, "", "OWNER", "일반"),
    ]
    paths = [next(a for a in c if a.startswith("repos/")) for c in calls]
    assert paths == ["repos/o/r/pulls/5/comments", "repos/o/r/pulls/5/reviews",
                     "repos/o/r/issues/5/comments"]  # fmt: skip
    assert all(c[:2] == ["api", "--paginate"] and "--jq" in c for c in calls)


def test_list_feedback_jq_drops_replies_and_empty_reviews_and_ends_in_json() -> None:
    calls: list[list[str]] = []
    github.list_feedback("o/r", 5, _feedback_runner(calls))
    jq = {next(a for a in c if a.startswith("repos/")): c[c.index("--jq") + 1] for c in calls}
    assert "in_reply_to_id == null" in jq["repos/o/r/pulls/5/comments"]  # 답글은 뺀다
    assert '(.body // "") != ""' in jq["repos/o/r/pulls/5/reviews"]  # 빈 본문의 리뷰는 뺀다
    assert all(j.endswith("| @json") for j in jq.values())  # 한 줄에 하나


def test_list_feedback_failure_is_github_read_error() -> None:
    def broken(args: list[str]) -> str:
        raise github.GitHubReadError("gh api 실패")

    with pytest.raises(github.GitHubReadError):
        github.list_feedback("o/r", 5, broken)

    with pytest.raises(github.GitHubReadError):
        github.list_feedback("o/r", 5, lambda args: "not json\n")
    with pytest.raises(github.GitHubReadError):
        github.list_feedback("o/r", 5, lambda args: '{"body": "id가 없다"}\n')
