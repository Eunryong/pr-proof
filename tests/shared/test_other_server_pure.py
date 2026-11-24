"""같은 저장소를 보는 다른 서버: 판 견주기, 결과 코멘트의 표시, 검사할지 정하기."""

from datetime import UTC, datetime, timedelta
from typing import Final

import pytest

from app.shared.comment_pure import (
    COMMENT_MARKER,
    REPLY_MARKER,
    command_in_comment,
    read_owner,
    result_marker,
)
from app.shared.feedback_pure import feedback_in_comment
from app.shared.other_server_pure import (
    DEFER_WAIT,
    Owner,
    Server,
    Verdict,
    compare_versions,
    decide,
    should_recommend,
    skips,
)

ME: Final = Server("aaaa1111", "0.1.3")
OTHER: Final = "bbbb2222"
HEAD: Final = "a" * 40
OLD_HEAD: Final = "b" * 40
NOW: Final = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


# bin/pr-proof.js의 compareVersions가 같은 쌍에 내는 값 (읽지 못하는 판은 null → 같음)
@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("0.1.3", "0.1.3", 0),
        ("0.1.3-beta.0", "0.1.3", -1),
        ("0.1.3", "0.1.3-beta.0", 1),
        ("0.1.3-beta.0", "0.1.3-beta.1", -1),
        ("0.1.3-beta.2", "0.1.3-beta.10", -1),
        ("0.1.3-alpha", "0.1.3-beta", -1),
        ("0.1.3-1", "0.1.3-beta", -1),
        ("0.1.3-beta", "0.1.3-beta.1", -1),
        ("0.2.0", "0.10.0", -1),
        ("1.0.0", "0.99.99", 1),
        ("v1.0.0", "1.0.0", 0),
        ("1.0.0+build", "1.0.0", 0),
        ("0.1.3-rc.1", "0.1.3-beta.9", 1),
        ("", "1.0.0", 0),
        ("x", "1.0.0", 0),
        ("1.0", "1.0.0", 0),
    ],
)
def test_compare_versions_matches_the_entry_point(a: str, b: str, expected: int) -> None:
    assert compare_versions(a, b) == expected


# --- 표시 ---


def test_marker_round_trip_and_stays_hidden() -> None:
    line = result_marker(OTHER, "0.1.3-beta.0", HEAD)
    assert line == (
        f"<!-- pr-proof --><!-- pr-proof:server id={OTHER} version=0.1.3-beta.0 head={HEAD} -->"
    )
    assert line.startswith(COMMENT_MARKER) and REPLY_MARKER not in line
    # HTML 주석 둘뿐이라 GitHub 화면에 보이는 글이 없다
    assert line.replace(COMMENT_MARKER, "").startswith("<!--") and line.endswith("-->")
    assert read_owner(f"{line}\n본문") == Owner(OTHER, "0.1.3-beta.0", HEAD)


@pytest.mark.parametrize(
    "text",
    [
        COMMENT_MARKER,  # 서버 정보가 없는 옛 표시
        f"{COMMENT_MARKER}\n본문",
        "",
        # 표시 없이 서버 줄만 흉내 낸 글
        f"<!-- pr-proof:server id={OTHER} version=0.1.3 head={HEAD} -->",
        # 꼴이 맞지 않는 값
        f"{COMMENT_MARKER}<!-- pr-proof:server id=XYZ version=0.1.3 head={HEAD} -->",
        f"{COMMENT_MARKER}<!-- pr-proof:server id={OTHER} version=0.1.3 head=abc -->",
    ],
)
def test_old_or_broken_markers_have_no_owner(text: str) -> None:
    assert read_owner(text) is None


def test_unreadable_version_is_left_empty() -> None:
    line = result_marker(OTHER, "1.0 -->x", HEAD)
    assert read_owner(line) == Owner(OTHER, "", HEAD)


def test_new_marker_is_still_the_pipelines_own_comment() -> None:
    body = f"{result_marker(OTHER, '0.1.3', HEAD)}\n/recheck"
    assert command_in_comment(body, "OWNER") == (None, "파이프라인 자신의 코멘트")
    assert feedback_in_comment(body, "OWNER", "me")[0] is False


# --- 판단 ---


def _owner(version: str, head: str = HEAD, server: str = OTHER) -> Owner:
    return Owner(server, version, head)


def _decide(owner: Owner | None, *, claim: bool = False, deferred_at: datetime | None = None):
    return decide(owner, ME, HEAD, claim=claim, deferred_at=deferred_at, now=NOW)


@pytest.mark.parametrize(
    "owner", [None, _owner("9.9.9", server=ME.id), _owner("0.0.1", OLD_HEAD, server=ME.id)],
    ids=["no-comment-or-old-marker", "mine", "mine-old-commit"],
)  # fmt: skip
def test_checks_when_no_other_server_owns_the_comment(owner: Owner | None) -> None:
    assert _decide(owner) == Verdict("check")


@pytest.mark.parametrize("head", [HEAD, OLD_HEAD])
def test_takes_over_from_a_lower_version(head: str) -> None:
    owner = _owner("0.1.3-beta.0", head)
    assert _decide(owner) == Verdict("take_over", owner, -1)


@pytest.mark.parametrize("version", ["0.1.3", "0.2.0", "", "엉뚱한 값"])
def test_same_or_higher_version_on_this_commit_already_did_it(version: str) -> None:
    owner = _owner(version)
    verdict = _decide(owner)
    assert verdict.action == "elsewhere" and verdict.other == owner
    assert verdict.relation == (1 if version == "0.2.0" else 0)


@pytest.mark.parametrize("version", ["0.1.3", "0.2.0"])
def test_same_or_higher_version_on_another_commit_is_deferred(version: str) -> None:
    owner = _owner(version, OLD_HEAD)
    assert _decide(owner).action == "defer"
    # 미룬 뒤 기다리는 시간이 지나기 전에는 계속 미룬다
    assert _decide(owner, deferred_at=NOW - DEFER_WAIT + timedelta(seconds=1)).action == "defer"


def test_deferred_too_long_takes_over() -> None:
    owner = _owner("0.2.0", OLD_HEAD)
    verdict = _decide(owner, deferred_at=NOW - DEFER_WAIT)
    assert verdict == Verdict("take_over", owner, 1, timed_out=True)
    # SQLite에서 읽은 시간대 없는 값은 UTC로 본다
    naive = (NOW - DEFER_WAIT).replace(tzinfo=None)
    assert _decide(owner, deferred_at=naive).timed_out


def test_deferred_but_the_other_server_did_this_commit_meanwhile() -> None:
    owner = _owner("0.2.0", HEAD)
    assert _decide(owner, deferred_at=NOW - 2 * DEFER_WAIT).action == "elsewhere"


@pytest.mark.parametrize("owner", [_owner("0.2.0"), _owner("0.1.3", OLD_HEAD)])
def test_a_human_recheck_always_checks_and_takes_over(owner: Owner) -> None:
    verdict = _decide(owner, claim=True)
    assert verdict.action == "take_over" and not skips(verdict)


def test_what_is_recommended() -> None:
    assert should_recommend(_decide(_owner("0.2.0")))  # 이미 함
    assert should_recommend(_decide(_owner("0.1.3", OLD_HEAD)))  # 미룸
    assert should_recommend(_decide(_owner("0.1.0")))  # 낮은 판을 넘겨받음
    assert not should_recommend(_decide(None))
    assert not should_recommend(_decide(_owner("0.2.0"), claim=True))
    assert not should_recommend(_decide(_owner("0.2.0", OLD_HEAD), deferred_at=NOW - DEFER_WAIT))
