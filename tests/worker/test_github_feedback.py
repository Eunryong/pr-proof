"""GitHub의 PR 코멘트 읽기·쓰기 (스레드, 리뷰, 답글, resolve_thread). gh는 가짜 runner."""

import json
from typing import Final

import pytest

from app.shared.feedback_pure import FEEDBACK_MARKER
from app.worker.github import GitHub, GitHubError, ReviewBody, ReviewThread, ThreadComment


class Recorder:
    def __init__(self, out: str = "") -> None:
        self.out = out
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, args: list[str], stdin: str | None) -> str:
        self.calls.append((args, stdin))
        return self.out


THREADS: Final = {
    "data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": [
        {
            "id": "PRRT_a", "isResolved": False, "isOutdated": False,
            "path": "src/a.py", "line": 12,
            "comments": {"nodes": [
                {"databaseId": 101, "author": {"login": "copilot-pull-request-reviewer"},
                 "authorAssociation": "NONE", "body": "위험합니다"},
                {"databaseId": 102, "author": {"login": "kim"},
                 "authorAssociation": "MEMBER", "body": "동의"},
            ]},
        },
        {
            "id": "PRRT_b", "isResolved": True, "isOutdated": True,
            "path": "src/b.py", "line": None,
            "comments": {"nodes": [
                {"databaseId": 201, "author": None, "authorAssociation": "NONE", "body": None},
            ]},
        },
    ]}}}}
}  # fmt: skip


def test_review_threads_sends_the_graphql_query_with_typed_variables() -> None:
    run = Recorder(json.dumps(THREADS))
    GitHub(run).review_threads("Eunryong/MathLLM", 7)
    ((args, stdin),) = run.calls
    assert args[:2] == ["api", "graphql"] and stdin is None
    query = next(a for a in args if a.startswith("query="))
    for word in ("reviewThreads(first: 100)", "isResolved", "isOutdated", "path", "line",
                 "comments(first: 50)", "databaseId", "author { login }", "authorAssociation",
                 "body"):  # fmt: skip
        assert word in query
    assert "mutation" not in query  # 읽기만 한다
    assert args.count("-f") == 1  # query는 문자열이라 -f, 나머지는 -F(숫자는 숫자로)
    assert ["-F", "owner=Eunryong"] == args[args.index("owner=Eunryong") - 1 :][:2]
    assert "name=MathLLM" in args and "number=7" in args
    assert args[args.index("number=7") - 1] == "-F"


def test_review_threads_parses_threads_and_comments() -> None:
    threads = GitHub(Recorder(json.dumps(THREADS))).review_threads("o/r", 7)
    assert threads == [
        ReviewThread(
            id="PRRT_a", resolved=False, outdated=False, path="src/a.py", line=12,
            comments=(
                ThreadComment(101, "copilot-pull-request-reviewer", "NONE", "위험합니다"),
                ThreadComment(102, "kim", "MEMBER", "동의"),
            ),
        ),
        ReviewThread(
            id="PRRT_b", resolved=True, outdated=True, path="src/b.py", line=None,
            comments=(ThreadComment(201, "", "NONE", ""),),  # 지운 계정, 빈 본문
        ),
    ]  # fmt: skip


@pytest.mark.parametrize(
    "out",
    ["", "not json", '{"data": null}', json.dumps({"errors": [{"message": "Bad"}]})],
    ids=["empty", "not-json", "no-repository", "graphql-errors"],
)
def test_review_threads_bad_response_is_github_error(out: str) -> None:
    with pytest.raises(GitHubError):
        GitHub(Recorder(out)).review_threads("o/r", 7)


def test_reviews_and_issue_comments_read_with_paginate_and_json_lines() -> None:
    line = json.dumps({"id": 5, "login": "kim", "association": "MEMBER", "body": "여러\n줄"})
    run = Recorder(line + "\n\n")
    assert GitHub(run).reviews("o/r", 3) == [ReviewBody(5, "kim", "MEMBER", "여러\n줄")]
    comments = GitHub(run).issue_comments("o/r", 3)
    assert [(c.id, c.login, c.association, c.body) for c in comments] == [
        (5, "kim", "MEMBER", "여러\n줄")
    ]
    (reviews_args, _), (comments_args, _) = run.calls
    assert reviews_args[:3] == ["api", "--paginate", "repos/o/r/pulls/3/reviews"]
    assert comments_args[:3] == ["api", "--paginate", "repos/o/r/issues/3/comments"]
    reviews_jq = reviews_args[reviews_args.index("--jq") + 1]
    assert '(.body // "") != ""' in reviews_jq  # 빈 본문의 리뷰는 뺀다
    assert reviews_jq.endswith("| @json")


def test_reviews_bad_json_is_github_error() -> None:
    with pytest.raises(GitHubError):
        GitHub(Recorder("not json\n")).reviews("o/r", 3)


def test_reply_to_review_comment_posts_to_the_thread_with_the_marker() -> None:
    run = Recorder("777\n")
    assert GitHub(run).reply_to_review_comment("o/r", 3, 101, "받아들였습니다") == 777
    ((args, stdin),) = run.calls
    assert args[:4] == ["api", "-X", "POST", "repos/o/r/pulls/3/comments/101/replies"]
    assert json.loads(stdin)["body"] == f"{FEEDBACK_MARKER}\n받아들였습니다"


def test_feedback_reply_is_a_plain_comment_with_the_marker() -> None:
    run = Recorder("778\n")
    assert GitHub(run).feedback_reply("o/r", 3, "답") == 778
    ((args, stdin),) = run.calls
    assert "repos/o/r/issues/3/comments" in args and "-X" not in args  # 새 코멘트는 POST가 기본
    assert json.loads(stdin)["body"] == f"{FEEDBACK_MARKER}\n답"


def test_resolve_thread_sends_the_mutation() -> None:
    run = Recorder(json.dumps({"data": {"resolveReviewThread": {"thread": {"isResolved": True}}}}))
    GitHub(run).resolve_thread("o/r", "PRRT_a")
    ((args, _),) = run.calls
    assert args[:2] == ["api", "graphql"]
    assert "resolveReviewThread(input: {threadId: $id})" in next(
        a for a in args if a.startswith("query=")
    )
    assert args[args.index("id=PRRT_a") - 1] == "-f"  # 노드 id는 문자열이다


@pytest.mark.parametrize("out", ["not json", json.dumps({"errors": [{"message": "no"}]})])
def test_resolve_thread_failure_is_github_error(out: str) -> None:
    with pytest.raises(GitHubError):
        GitHub(Recorder(out)).resolve_thread("o/r", "PRRT_a")
