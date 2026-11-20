"""models/access.py: 외부 작성자 PR 가드레일의 판단 (DESIGN 8.6, 완료 기준 6-8)."""

from typing import Final

import pytest

from app.models.access import (
    AccessSettings,
    Allowed,
    BadCommit,
    Commit,
    NeedsApproval,
    NoCommit,
    approval_arg,
    may_approve,
    may_run,
    parse_access,
)

HEAD: Final = "abc1234" + "0" * 33
OTHER: Final = "def5678" + "0" * 33
DEFAULT: Final = AccessSettings()
WITH_BOT: Final = AccessSettings(bots=frozenset({"dependabot[bot]"}))
UNTRUSTED: Final = [
    "CONTRIBUTOR",
    "FIRST_TIME_CONTRIBUTOR",
    "FIRST_TIMER",
    "NONE",
    "MANNEQUIN",
    "SOMETHING_NEW",
    "owner",  # 대소문자가 다른 값도 모르는 값이다
    "",
    None,
]


@pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_default_trusted_values_run(association: str) -> None:
    assert may_run(association, "someone", HEAD, DEFAULT, ()) == Allowed("trusted")


@pytest.mark.parametrize("association", UNTRUSTED)
def test_everyone_else_needs_approval(association: str | None) -> None:
    assert may_run(association, "someone", HEAD, DEFAULT, ()) == NeedsApproval(HEAD)


def test_trusted_list_comes_from_settings() -> None:
    owner_only = AccessSettings(trusted=frozenset({"OWNER"}))
    assert may_run("OWNER", "a", HEAD, owner_only, ()) == Allowed("trusted")
    assert may_run("MEMBER", "a", HEAD, owner_only, ()) == NeedsApproval(HEAD)
    with_contributors = AccessSettings(trusted=frozenset({"OWNER", "CONTRIBUTOR"}))
    assert may_run("CONTRIBUTOR", "a", HEAD, with_contributors, ()) == Allowed("trusted")
    nobody = AccessSettings(trusted=frozenset())
    assert may_run("OWNER", "a", HEAD, nobody, ()) == NeedsApproval(HEAD)


def test_listed_bot_runs_and_others_do_not() -> None:
    assert may_run("NONE", "dependabot[bot]", HEAD, WITH_BOT, ()) == Allowed("bot")
    assert may_run("NONE", "renovate[bot]", HEAD, WITH_BOT, ()) == NeedsApproval(HEAD)
    assert may_run("NONE", "dependabot[bot]", HEAD, DEFAULT, ()) == NeedsApproval(HEAD)
    assert may_run("NONE", "", HEAD, WITH_BOT, ()) == NeedsApproval(HEAD)
    assert may_run("NONE", None, HEAD, WITH_BOT, ()) == NeedsApproval(HEAD)


def test_only_the_approved_commit_runs() -> None:
    assert may_run("NONE", "x", HEAD, DEFAULT, {HEAD}) == Allowed("approved")
    assert may_run("NONE", "x", OTHER, DEFAULT, {HEAD}) == NeedsApproval(OTHER)
    # 짧은 해시나 빈 값은 승인 기록과 같아도 통과하지 않는다
    assert may_run("NONE", "x", "", DEFAULT, {""}) == NeedsApproval("")
    assert may_run("NONE", "x", HEAD[:7], DEFAULT, {HEAD[:7]}) == NeedsApproval(HEAD[:7])


def test_bots_cannot_approve() -> None:
    assert may_approve("MEMBER", "maintainer", WITH_BOT)
    assert not may_approve("MEMBER", "dependabot[bot]", WITH_BOT)
    # 목록에 없는 봇도, 저장소 주인 관계로 보여도 승인하지 못한다
    assert not may_approve("OWNER", "some-app[bot]", DEFAULT)


@pytest.mark.parametrize("association", UNTRUSTED)
def test_untrusted_people_cannot_approve(association: str | None) -> None:
    assert not may_approve(association, "someone", DEFAULT)


def test_approver_needs_a_login() -> None:
    assert not may_approve("OWNER", "", DEFAULT)
    assert not may_approve("OWNER", None, DEFAULT)


@pytest.mark.parametrize(
    ("arg", "expected"),
    [
        ("", NoCommit()),
        ("   ", NoCommit()),
        ("abc1234", Commit("abc1234")),
        ("ABC1234", Commit("abc1234")),
        ("abc1234 확인했습니다", Commit("abc1234")),
        (HEAD, Commit(HEAD)),
        ("abc123", BadCommit()),  # 7자 미만
        ("abc123g", BadCommit()),  # 16진수가 아님
        (HEAD + "0", BadCommit()),  # 40자 초과
        ("`abc1234`", BadCommit()),
    ],
)
def test_approval_arg(arg: str, expected: object) -> None:
    assert approval_arg(arg) == expected


def test_commit_matches_only_the_head_it_prefixes() -> None:
    assert Commit("abc1234").matches(HEAD)
    assert Commit(HEAD).matches(HEAD)
    assert not Commit("abc1234").matches(OTHER)
    assert not Commit("abc1234").matches("")
    assert not Commit("abc1234").matches("abc1234")  # head가 전체 해시가 아니면 맞지 않는다


def test_settings_default_without_access() -> None:
    assert parse_access(None) == (DEFAULT, ())
    assert parse_access("sandbox:\n  memory_gb: 2\n") == (DEFAULT, ())
    assert parse_access(": : bad yaml") == (DEFAULT, ())


def test_settings_read_trusted_and_bots() -> None:
    access, notes = parse_access(
        "access:\n  trusted: [owner, MEMBER]\n  bots: ['dependabot[bot]']\n"
    )
    assert access == AccessSettings(frozenset({"OWNER", "MEMBER"}), frozenset({"dependabot[bot]"}))
    assert notes == ()


def test_unrelated_errors_do_not_reset_access() -> None:
    access, _ = parse_access("sandbox:\n  memory_gb: -1\naccess:\n  trusted: [OWNER]\n")
    assert access.trusted == frozenset({"OWNER"})


def test_empty_trusted_list_trusts_nobody() -> None:
    access, notes = parse_access("access:\n  trusted: []\n")
    assert access.trusted == frozenset() and notes == ()


def test_unknown_values_and_non_bot_logins_are_dropped_with_a_note() -> None:
    access, notes = parse_access(
        "access:\n  trusted: [OWNER, OWNR, '@someone']\n  bots: [alice, '[bot]', 'x[bot]']\n"
    )
    assert access == AccessSettings(frozenset({"OWNER"}), frozenset({"x[bot]"}))
    assert len(notes) == 2
    # 설정에 적힌 글은 코멘트에 옮기지 않는다 (T10)
    assert not any("@someone" in n or "alice" in n for n in notes)


@pytest.mark.parametrize(
    "text",
    [
        "access: [OWNER]\n",
        "access:\n  trusted: OWNER\n",
        "access:\n  trusted: [1, 2]\n",
        "access:\n  bots: dependabot[bot]\n",
    ],
)
def test_malformed_access_falls_back_to_defaults_with_a_note(text: str) -> None:
    access, notes = parse_access(text)
    assert access == DEFAULT and len(notes) == 1
