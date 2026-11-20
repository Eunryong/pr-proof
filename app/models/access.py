"""외부 작성자 PR 가드레일의 판단 (docs/DESIGN.md 8.6, 위협 표 T2·T18). 외부 호출 없음.

신뢰되지 않은 사람이 연 PR에는 신뢰된 사람이 그 커밋을 승인할 때까지 아무것도 돌리지 않는다.
워커가 모든 작업의 시작에서, GitHub의 PR 조회로 방금 읽은 값으로 `may_run` 하나를 부른다.
`PRJob`에 실린 값은 기록용이라 여기에 넘기지 않는다.
"""

import re
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any, Final, Literal

import yaml

DEFAULT_TRUSTED: Final = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
# GitHub이 주는 author_association 값. 설정의 `access.trusted`는 이 안의 값만 받는다
ASSOCIATIONS: Final = frozenset(
    {
        "OWNER",
        "MEMBER",
        "COLLABORATOR",
        "CONTRIBUTOR",
        "FIRST_TIME_CONTRIBUTOR",
        "FIRST_TIMER",
        "MANNEQUIN",
        "NONE",
    }
)
# GitHub 로그인 이름에는 `[`가 들어갈 수 없어, 이 꼬리가 붙은 이름은 GitHub App(봇)만 쓴다
BOT_SUFFIX: Final = "[bot]"
# 승인은 전체 해시만 받는다. 짧은 해시는 앞자리가 같은 커밋을 만들어 바꿔 끼울 수 있다 (T31)
_FULL_SHA: Final = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class AccessSettings:
    """base `.pipeline.yaml`의 `access` (10절). PR head의 값은 쓰지 않는다 (8.3)."""

    trusted: frozenset[str] = DEFAULT_TRUSTED
    bots: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class Author:
    """PR 작성자. 워커가 GitHub의 PR 조회에서 읽은 값이다."""

    association: str
    login: str


@dataclass(frozen=True)
class Allowed:
    reason: Literal["trusted", "bot", "approved"]


@dataclass(frozen=True)
class NeedsApproval:
    head_sha: str  # 승인 안내에 전체 해시로 보일 지금 head


Decision = Allowed | NeedsApproval


def may_run(
    author_association: str | None,
    author_login: str | None,
    head_sha: str,
    access: AccessSettings,
    approved_shas: Collection[str],
) -> Decision:
    """워커가 GitHub의 PR 조회에서 방금 읽은 값으로만 부른다. 없거나 모르는 값은 신뢰되지 않는다."""
    if author_association in access.trusted:
        return Allowed("trusted")
    if author_login and author_login in access.bots:
        return Allowed("bot")
    if _FULL_SHA.fullmatch(head_sha) and head_sha in approved_shas:
        return Allowed("approved")
    return NeedsApproval(head_sha)


def may_approve(association: str | None, login: str | None, access: AccessSettings) -> bool:
    """승인 명령을 단 사람이 승인할 수 있는지. 값은 워커가 GitHub에서 읽은 코멘트의 것이다.

    봇은 `access.bots`에 있든 없든, 저장소와의 관계가 무엇이든 승인하지 못한다.
    """
    if not login or login.endswith(BOT_SUFFIX) or login in access.bots:
        return False
    return association in access.trusted


@dataclass(frozen=True)
class NoCommit:
    """`/recheck`에 커밋을 적지 않았다."""


@dataclass(frozen=True)
class BadCommit:
    """커밋이 16진수 40자(전체 해시)가 아니다. 짧은 해시도 여기다 (T31)."""


@dataclass(frozen=True)
class Commit:
    sha: str  # 소문자 16진수 40자

    def matches(self, head_sha: str) -> bool:
        """이 커밋이 지금 head인지. 해시만 본다. 커밋 시각은 작성자가 정할 수 있어 쓰지 않는다."""
        return head_sha == self.sha


ApprovalArg = NoCommit | BadCommit | Commit


def approval_arg(arg: str) -> ApprovalArg:
    """`/recheck <커밋>`의 인자. 첫 낱말만 커밋으로 본다."""
    words = arg.split()
    if not words:
        return NoCommit()
    word = words[0].lower()
    return Commit(word) if _FULL_SHA.fullmatch(word) else BadCommit()


def parse_access(text: str | None) -> tuple[AccessSettings, tuple[str, ...]]:
    """(설정, "설정:"에 알릴 문제). 파일을 읽지 못한 이유는 `parse_repo_config`가 따로 알린다.

    `access`는 다른 항목과 따로 읽는다. 다른 항목의 오류로 신뢰 목록이 기본값으로 바뀌지 않는다.
    `access` 자체의 모양이 틀리면 기본값(OWNER·MEMBER·COLLABORATOR, 봇 없음)을 쓴다.
    """
    try:
        data = yaml.safe_load(text) if text else None
    except yaml.YAMLError:
        return AccessSettings(), ()
    section = data.get("access") if isinstance(data, dict) else None
    if section is None:
        return AccessSettings(), ()
    if not isinstance(section, dict):
        return AccessSettings(), ("`access`가 매핑이 아니라 기본값을 씀",)
    notes: list[str] = []
    trusted = _trusted(section.get("trusted"), notes)
    bots = _bots(section.get("bots"), notes)
    return AccessSettings(trusted=trusted, bots=bots), tuple(notes)


def _strings(value: Any, key: str, notes: list[str]) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        notes.append(f"`access.{key}`가 문자열 목록이 아니라 기본값을 씀")
        return None
    return [v.strip() for v in value]


def _trusted(value: Any, notes: list[str]) -> frozenset[str]:
    if value is None:
        return DEFAULT_TRUSTED
    items = _strings(value, "trusted", notes)
    if items is None:
        return DEFAULT_TRUSTED
    upper = [v.upper() for v in items]
    # 값은 코멘트에 옮기지 않는다 (T10). 개수만 알린다
    unknown = [v for v in upper if v not in ASSOCIATIONS]
    if unknown:
        notes.append(f"`access.trusted`의 GitHub이 쓰지 않는 값 {len(unknown)}개는 쓰지 않음")
    return frozenset(v for v in upper if v in ASSOCIATIONS)


def _bots(value: Any, notes: list[str]) -> frozenset[str]:
    if value is None:
        return frozenset()
    items = _strings(value, "bots", notes)
    if items is None:
        return frozenset()
    not_bots = [v for v in items if not _is_bot_login(v)]
    if not_bots:
        notes.append(
            f"`access.bots`에는 `{BOT_SUFFIX}`로 끝나는 봇 계정만 적습니다. "
            f"그렇지 않은 {len(not_bots)}개는 쓰지 않음"
        )
    return frozenset(v for v in items if _is_bot_login(v))


def _is_bot_login(login: str) -> bool:
    return len(login) > len(BOT_SUFFIX) and login.endswith(BOT_SUFFIX)
