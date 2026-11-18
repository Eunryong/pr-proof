import json

import pytest

from app.models.comment import COMMENT_MARKER
from app.worker.github import GitHub, GitHubError


class FakeGh:
    def __init__(
        self, comments: str = "", patch_error: str | None = None, head: str = "a" * 40
    ) -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.comments = comments
        self.patch_error = patch_error
        self.head = head  # PR의 지금 head (pr_head)
        self.head_error: str | None = None

    def __call__(self, args: list[str], stdin: str | None) -> str:
        self.calls.append((args, stdin))
        if args[:2] == ["api", "user"]:
            return "me\n"
        if _is_head_lookup(args):
            if self.head_error:
                raise GitHubError(self.head_error)
            return self.head + "\n"
        if "--paginate" in args:
            return self.comments
        if "PATCH" in args:
            if self.patch_error:
                raise GitHubError(self.patch_error)
            return "1\n"
        return "555\n"  # 새 코멘트 ID

    def kinds(self) -> list[str]:
        return ["patch" if "PATCH" in a else "find" if "--paginate" in a else "create"
                for a, _ in self.calls
                if a[:2] != ["api", "user"] and not _is_head_lookup(a)]  # fmt: skip


def _is_head_lookup(args: list[str]) -> bool:
    return len(args) > 1 and "/pulls/" in args[1] and ".head.sha" in args


def test_pr_head() -> None:
    gh = FakeGh(head="b" * 40)
    assert GitHub(gh).pr_head("o/r", 7) == "b" * 40
    assert gh.calls[0][0][:2] == ["api", "repos/o/r/pulls/7"]


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
