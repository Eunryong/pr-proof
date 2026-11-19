import json

import pytest

from app.models.comment import COMMENT_MARKER, REPLY_MARKER
from app.worker.github import GitHub, GitHubError


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

    def kinds(self) -> list[str]:
        """원본 PR(#1)의 결과 코멘트에 대한 호출만."""
        out = []
        for a, _ in self.calls:
            joined = " ".join(a)
            if "issues/1/comments" not in joined and "issues/comments/" not in joined:
                continue
            out.append("patch" if "PATCH" in a else "find" if "--paginate" in a else "create")
        return out


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
