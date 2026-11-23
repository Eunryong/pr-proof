import json
import re

import pytest

from app.shared.comment_pure import COMMENT_MARKER, REPLY_MARKER
from app.worker.github import GitHub, GitHubError, ReviewComment


class FakeGh:
    """gh 대역. 원본 PR(#1)의 코멘트와 제안 PR(900번대)을 흉내 낸다."""

    def __init__(
        self,
        comments: str = "",
        patch_error: str | None = None,
        head: str = "a" * 40,
        base: str = "b" * 40,
    ) -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.comments = comments
        self.patch_error = patch_error
        self.head = head  # PR의 지금 head·base (pr_refs)
        self.base = base
        self.state = "open"
        self.head_ref = "feature"
        self.head_repo = "o/r"
        self.author_association = "OWNER"
        self.author_login = "author"
        self.head_error: str | None = None
        # 제안 PR: 번호 → {"state", "merged", "merge_commit", "title", "body", "head", "base"}
        self.pulls: dict[int, dict] = {}
        self.deleted_branches: list[str] = []
        # 제안 PR의 리뷰: 번호 → [{"body", "event", "comments", "login"}]
        self.reviews: dict[int, list[dict]] = {}
        self.review_error: str | None = None
        # 원본 PR의 코멘트 하나 조회 (승인자 확인): ID → {"login", "association", "body", "pr"}
        self.issue_comments: dict[int, dict] = {}
        # 원본 PR의 제목과 본문 (PR 단위 확인의 pr_text). 조회한 횟수를 센다
        self.title = "PR 제목"
        self.body = ""
        self.text_lookups = 0
        # 원본 PR이 닫겠다고 한 이슈 (이슈 확인의 GraphQL 조회). 기본은 없음이라 모델을 부르지
        # 않는다.
        # 노드 모양은 GitHub의 응답 그대로다 (`linked_node`)
        self.linked: list[dict | None] = []
        self.linked_error: str | None = None
        self.linked_lookups = 0

    def __call__(self, args: list[str], stdin: str | None) -> str:
        self.calls.append((args, stdin))
        path = args[1] if len(args) > 1 and not args[1].startswith("-") else ""
        method = args[args.index("-X") + 1] if "-X" in args else "GET"
        if "-X" in args:
            path = args[args.index("-X") + 2]
        if args[:2] == ["api", "user"]:
            return "42\tme\n" if "[.id, .login] | @tsv" in args else "me\n"
        if _is_head_lookup(args):
            if self.head_error:
                raise GitHubError(self.head_error)
            return (
                f"{self.head}\t{self.base}\t{self.state}\t{self.head_ref}\t{self.head_repo}"
                f"\t{self.author_association}\t{self.author_login}\n"
            )
        if args[:2] == ["api", "graphql"] and any("closingIssuesReferences" in x for x in args):
            self.linked_lookups += 1
            if self.linked_error:
                raise GitHubError(self.linked_error)
            refs = {"totalCount": len(self.linked), "nodes": self.linked}
            return json.dumps({"data": {"repository": {"pullRequest": {
                "closingIssuesReferences": refs}}}}) + "\n"  # fmt: skip
        if _is_text_lookup(args):
            self.text_lookups += 1
            return (
                json.dumps({"title": self.title, "body": self.body, "branch": self.head_ref}) + "\n"
            )
        one_comment = re.fullmatch(r"repos/o/r/issues/comments/(\d+)", path)
        if one_comment and method == "GET":
            c = self.issue_comments.get(int(one_comment.group(1)))
            if c is None:
                raise GitHubError("HTTP 404: Not Found")
            return json.dumps({
                "login": c["login"], "association": c["association"], "body": c["body"],
                "issue_url": f"https://api.github.com/repos/o/r/issues/{c.get('pr', 1)}",
            }) + "\n"  # fmt: skip
        review = re.fullmatch(r"repos/o/r/pulls/(\d+)/reviews", path or args[2])
        if review:
            return self._review(int(review.group(1)), method, args, stdin)
        if path.startswith("repos/o/r/pulls?"):
            branch = path.split("head=o:")[1]
            found = [
                n for n, p in self.pulls.items() if p["head"] == branch and p["state"] == "open"
            ]
            return f"{found[0]}\n" if found else ""
        if path == "repos/o/r/pulls" and method == "POST":
            number = 900 + len(self.pulls)
            data = json.loads(stdin)
            self.pulls[number] = data | {"state": "open", "merged": False, "merge_commit": ""}
            return f"{number}\n"
        if path.startswith("repos/o/r/pulls/") and "/pulls/1" != path[-8:]:
            number = int(path.rsplit("/", 1)[1])
            pull = self.pulls[number]
            if method == "PATCH":
                pull.update(json.loads(stdin))
                return f"{number}\n"
            merged = "true" if pull["merged"] else "false"
            return f"{pull['state']}\t{merged}\t{pull['merge_commit']}\n"
        if method == "DELETE":
            self.deleted_branches.append(path.split("refs/heads/")[1])
            return ""
        if "--paginate" in args:
            return self.comments
        if "PATCH" in args:
            if self.patch_error:
                raise GitHubError(self.patch_error)
            return "1\n"
        return "555\n"  # 새 코멘트 ID

    def _review(self, number: int, method: str, args: list[str], stdin: str | None) -> str:
        if method == "POST":
            if self.review_error:
                raise GitHubError(self.review_error)
            self.reviews.setdefault(number, []).append(json.loads(stdin) | {"login": "me"})
            return f"{700 + len(self.reviews[number])}\n"
        marker = re.search(r'contains\("([^"]+)"\)', args[args.index("--jq") + 1]).group(1)
        return "".join(
            f"{700 + i}\t{r['login']}\n"
            for i, r in enumerate(self.reviews.get(number, []), 1)
            if marker in r["body"]
        )

    def kinds(self) -> list[str]:
        """원본 PR(#1)의 결과 코멘트에 대한 호출만."""
        out = []
        for a, _ in self.calls:
            joined = " ".join(a)
            if "issues/1/comments" not in joined and "issues/comments/" not in joined:
                continue
            out.append("patch" if "PATCH" in a else "find" if "--paginate" in a else "create")
        return out


def linked_node(
    number: int, title: str, body: str = "", comments: tuple[tuple[str, str], ...] = (),
    repo: str = "o/r",
) -> dict:  # fmt: skip
    """`closingIssuesReferences`의 이슈 노드 하나. comments는 (작성자의 관계, 본문)이다."""
    return {
        "__typename": "Issue", "number": number, "title": title, "body": body,
        "repository": {"nameWithOwner": repo},
        "comments": {"totalCount": len(comments),
                     "nodes": [{"authorAssociation": a, "body": b} for a, b in comments]},
    }  # fmt: skip


def _is_text_lookup(args: list[str]) -> bool:
    return len(args) > 1 and "/pulls/" in args[1] and any(".title" in a for a in args)


def _is_head_lookup(args: list[str]) -> bool:
    return len(args) > 1 and "/pulls/" in args[1] and any(".head.sha" in a for a in args)


def is_reply(stdin: str | None) -> bool:
    return stdin is not None and REPLY_MARKER in stdin


def test_pr_refs() -> None:
    gh = FakeGh(head="c" * 40, base="d" * 40)
    refs = GitHub(gh).pr_refs("o/r", 7)
    assert (refs.head_sha, refs.base_sha, refs.open) == ("c" * 40, "d" * 40, True)
    assert gh.calls[0][0][:2] == ["api", "repos/o/r/pulls/7"]
    gh.state = "closed"
    assert not GitHub(gh).pr_refs("o/r", 7).open


@pytest.mark.parametrize("out", ["", "\n", "abc\tdef\topen\n", f"{'a' * 40}\t-x\topen\n"])
def test_unreadable_pr_lookup_is_a_failure(out: str) -> None:
    """빈 응답이나 해시가 아닌 head·base로 판단하지 않는다 (8.6)."""
    with pytest.raises(GitHubError):
        GitHub(lambda args, stdin: out).pr_refs("o/r", 7)


def test_issue_comment_reads_author_body_and_pr() -> None:
    gh = FakeGh()
    gh.issue_comments[5] = {"login": "m", "association": "MEMBER", "body": "/recheck a\nb", "pr": 3}
    comment = GitHub(gh).issue_comment("o/r", 5)
    assert (comment.login, comment.association, comment.body, comment.pr_number) == (
        "m", "MEMBER", "/recheck a\nb", 3,
    )  # fmt: skip
    assert gh.calls[0][0][:2] == ["api", "repos/o/r/issues/comments/5"]


@pytest.mark.parametrize(
    "out",
    [
        "",
        "not json",
        json.dumps({"login": "m"}),
        json.dumps({"login": "m", "association": "MEMBER", "body": "", "issue_url": None}),
    ],
)
def test_unreadable_issue_comment_is_a_failure(out: str) -> None:
    with pytest.raises(GitHubError):
        GitHub(lambda args, stdin: out).issue_comment("o/r", 5)


def test_issue_comment_of_another_repo_has_no_pr() -> None:
    out = json.dumps({"login": "m", "association": "MEMBER", "body": "",
                      "issue_url": "https://api.github.com/repos/x/y/issues/1"})  # fmt: skip
    assert GitHub(lambda args, stdin: out).issue_comment("o/r", 5).pr_number is None


def test_reply_uses_its_own_marker() -> None:
    """답글이 결과 코멘트로 잘못 찾히지 않게 한다."""
    gh = FakeGh()
    assert GitHub(gh).reply("o/r", 1, "본문") == 555
    body = json.loads(gh.calls[-1][1])["body"]
    assert body.startswith(REPLY_MARKER) and COMMENT_MARKER not in body


def test_creates_comment_with_marker_when_none_exists() -> None:
    gh = FakeGh()
    assert GitHub(gh).upsert_comment("o/r", 1, "본문", None) == 555
    assert gh.kinds() == ["find", "create"]
    assert json.loads(gh.calls[-1][1])["body"] == f"{COMMENT_MARKER}\n본문"


def test_updates_stored_comment_without_searching() -> None:
    gh = FakeGh()
    assert GitHub(gh).upsert_comment("o/r", 1, "본문", 42) == 42
    assert gh.kinds() == ["patch"]


def test_finds_own_marked_comment() -> None:
    gh = FakeGh(comments="10\tsomeone\n11\tme\n")  # 다른 사람이 표시를 흉내 낸 코멘트는 건너뜀
    assert GitHub(gh).upsert_comment("o/r", 1, "본문", None) == 11
    assert gh.kinds() == ["find", "patch"]


def test_deleted_comment_falls_back_to_create() -> None:
    gh = FakeGh(patch_error="gh api 실패: HTTP 404: Not Found")
    assert GitHub(gh).upsert_comment("o/r", 1, "본문", 42) == 555
    assert gh.kinds() == ["patch", "find", "create"]


def test_other_patch_errors_propagate() -> None:
    gh = FakeGh(patch_error="gh api 실패: HTTP 401: Bad credentials")
    with pytest.raises(GitHubError):
        GitHub(gh).upsert_comment("o/r", 1, "본문", 42)


def test_review_is_posted_once_per_patch_and_ignores_lookalikes() -> None:
    """같은 패치의 리뷰는 한 번만 단다. 다른 사람이 표시를 흉내 낸 리뷰는 우리 것이 아니다."""
    from app.worker.fix import proposal

    gh = FakeGh()
    github = GitHub(gh)
    marker = proposal.proposal_pure.review_marker("patch-1")
    gh.reviews[900] = [{"body": marker, "login": "someone"}]
    comments = [ReviewComment("a.py", 2, "규칙 `R` 이유 `w`")]
    proposal.post_review(github, "o/r", 900, comments, "patch-1", ())
    proposal.post_review(github, "o/r", 900, comments, "patch-1", ())
    mine = [r for r in gh.reviews[900] if r["login"] == "me"]
    assert len(mine) == 1 and mine[0]["body"].startswith(marker)
    assert mine[0]["comments"] == [{"path": "a.py", "line": 2, "side": "RIGHT",
                                    "body": "규칙 `R` 이유 `w`"}]  # fmt: skip
    # 패치가 바뀌면(다시 돌린 수정) 새 리뷰를 단다
    proposal.post_review(github, "o/r", 900, comments, "patch-2", ())
    assert len([r for r in gh.reviews[900] if r["login"] == "me"]) == 2


def test_review_with_a_secret_or_no_lines_is_not_posted() -> None:
    from app.worker.fix import proposal

    gh = FakeGh()
    secret = [ReviewComment("a.py", 2, "ghp_" + "A" * 36)]
    proposal.post_review(GitHub(gh), "o/r", 900, secret, "p", ())
    proposal.post_review(GitHub(gh), "o/r", 900, [], "p", ())
    assert gh.reviews == {}


def test_review_blocked_by_the_secondary_rate_limit_is_skipped() -> None:
    """GitHub이 2차 한도로 막으면 리뷰만 빠지고 전달은 이어 간다 (MathLLM#6, 2026-11-21)."""
    from app.worker.fix import proposal

    gh = FakeGh()
    gh.review_error = (
        "gh api -X 실패: gh: You have exceeded a secondary rate limit and have been temporarily "
        "blocked from content creation."
    )
    proposal.post_review(GitHub(gh), "o/r", 900, [ReviewComment("a.py", 1, "b")], "p", ())
    assert gh.reviews.get(900, []) == []


def test_review_errors_other_than_422_propagate() -> None:
    from app.worker.fix import proposal

    gh = FakeGh()
    gh.review_error = "gh api 실패: HTTP 502"
    with pytest.raises(GitHubError):
        proposal.post_review(GitHub(gh), "o/r", 900, [ReviewComment("a.py", 1, "b")], "p", ())


def test_linked_issues_reads_the_closing_issues_with_typed_variables() -> None:
    """PR이 닫겠다고 한 이슈 (이슈 확인, 4.5 끝). 응답 모양은 공개 저장소의 읽기 조회로 확인했다."""
    gh = FakeGh()
    gh.linked = [linked_node(7, "제목", "본문", (("MEMBER", "범위를 줄입니다"),)), None]
    linked = GitHub(gh).linked_issues("o/r", 1)
    ((args, stdin),) = gh.calls
    assert args[:2] == ["api", "graphql"] and stdin is None
    assert {"owner=o", "name=r", "number=1"} <= set(args) and args.count("-F") == 3
    assert "closingIssuesReferences(first: 10)" in args[3] and "comments(last: 50)" in args[3]
    (issue,) = linked.issues
    assert (issue.number, issue.repo, issue.title, issue.body) == (7, "o/r", "제목", "본문")
    assert [(c.association, c.body) for c in issue.comments] == [("MEMBER", "범위를 줄입니다")]
    assert (linked.total, linked.fetched, linked.unreadable) == (2, 2, 1)


@pytest.mark.parametrize("out", ["", "not json", "[]", json.dumps({"errors": [{"message": "x"}]})])
def test_unreadable_linked_issues_lookup_is_a_failure(out: str) -> None:
    with pytest.raises(GitHubError):
        GitHub(lambda args, stdin: out).linked_issues("o/r", 1)
