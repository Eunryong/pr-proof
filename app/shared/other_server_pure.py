"""같은 저장소를 보는 다른 pr-proof 서버를 알아채고 양보하는 규칙. 외부 호출 없음.

서버들은 서로 이야기하지 않는다. 같이 보는 곳은 결과 코멘트의 숨은 표시뿐이다
(`comment_pure.result_marker`). 워커는 검사를 시작하기 전에 그 표시를 읽어 `decide`로 검사할지
정하고, 동기화는 미룬 작업을 `DEFER_WAIT`가 지난 뒤 한 번 더 넣는다. 저장소를 목록에서 빼는 것은
사람이 한다.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

# 다른 서버가 아직 처리하지 않은 커밋을 미룬 뒤 기다리는 시간. 지나도 그 서버가 이 커밋을 처리하지
# 않았으면 그 서버가 꺼졌다고 보고 넘겨받는다
DEFER_WAIT: Final = timedelta(minutes=30)

# runs의 상태: 다른 서버가 이 커밋을 이미 처리해 검사하지 않음(끝), 다른 서버를 기다리며 미룸
ELSEWHERE: Final = "elsewhere"
DEFERRED: Final = "deferred"

# bin/pr-proof.js의 VERSION_RE와 같은 꼴
_VERSION: Final = re.compile(r"v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?")


@dataclass(frozen=True)
class Server:
    """이 서버. id는 데이터 폴더의 파일, version은 패키지의 판이다(`app/shared/server.py`)."""

    id: str
    version: str


@dataclass(frozen=True)
class Owner:
    """결과 코멘트의 표시에 적힌, 그 코멘트를 마지막으로 쓴 서버와 그 코멘트가 말하는 커밋."""

    server_id: str
    version: str
    head_sha: str


Action = Literal["check", "take_over", "elsewhere", "defer"]


@dataclass(frozen=True)
class Verdict:
    """check: 검사한다. take_over: 다른 서버의 코멘트를 넘겨받아 검사한다. elsewhere: 다른 서버가
    이 커밋을 이미 처리해 검사하지 않는다. defer: 다른 서버를 기다리며 지금은 검사하지 않는다.

    other는 다른 서버의 표시(다른 서버가 아니면 None), relation은 그 서버의 판을 이 서버의 판과
    견준 값(-1 낮음, 0 같음, 1 높음)이다. timed_out은 미룬 뒤 기다리는 시간이 지나 넘겨받는 경우다.
    """

    action: Action
    other: Owner | None = None
    relation: int = 0
    timed_out: bool = False


def _parse(text: str) -> tuple[tuple[int, int, int], list[str]] | None:
    m = _VERSION.fullmatch(text.strip())
    if m is None:
        return None
    pre = m.group(4).split(".") if m.group(4) else []
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))), pre


def compare_versions(a: str, b: str) -> int:
    """a가 b보다 낮으면 -1, 같으면 0, 높으면 1. 어느 쪽이든 판의 꼴이 아니면 0(같음)이다.

    `bin/pr-proof.js`의 `compareVersions`와 같은 순서다(semver 11절): 시험판 꼬리가 없는 판이
    더 높고, 꼬리끼리는 점으로 나눈 조각을 차례로 견준다. 숫자 조각은 숫자로 견주고 글자 조각보다
    낮으며, 앞이 모두 같으면 조각이 적은 쪽이 낮다.
    """
    x, y = _parse(a), _parse(b)
    if x is None or y is None:
        return 0
    if x[0] != y[0]:
        return -1 if x[0] < y[0] else 1
    xp, yp = x[1], y[1]
    if not xp or not yp:
        if len(xp) == len(yp):
            return 0
        return -1 if xp else 1
    for p, q in zip(xp, yp, strict=False):
        if p == q:
            continue
        if p.isdigit() and q.isdigit():
            return -1 if int(p) < int(q) else 1
        if p.isdigit() != q.isdigit():
            return -1 if p.isdigit() else 1
        return -1 if p < q else 1
    if len(xp) == len(yp):
        return 0
    return -1 if len(xp) < len(yp) else 1


def _utc(value: datetime) -> datetime:
    """SQLite는 시간대를 저장하지 않으므로 시간대 없는 값은 UTC로 본다."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def decide(
    owner: Owner | None,
    me: Server,
    head_sha: str,
    *,
    claim: bool,
    deferred_at: datetime | None,
    now: datetime,
) -> Verdict:
    """결과 코멘트의 표시(owner)로 이 커밋(head_sha)을 검사할지 정한다.

    owner가 None이면 코멘트가 없거나 서버 정보가 없는 옛 표시다. claim은 사람이 단 `/recheck`의
    작업이고 표시와 관계없이 검사한다. deferred_at은 이 커밋을 처음 미룬 때다(미룬 적이 없으면
    None). 판이 같거나 높은 다른 서버가 다른 커밋을 말하고 있으면 미루고, 미룬 뒤 `DEFER_WAIT`가
    지났는데도 그 서버가 이 커밋을 처리하지 않았으면 넘겨받는다.
    """
    if owner is None or owner.server_id == me.id:
        return Verdict("check")
    relation = compare_versions(owner.version, me.version)
    if claim or relation < 0:
        return Verdict("take_over", owner, relation)
    if owner.head_sha == head_sha:
        return Verdict("elsewhere", owner, relation)
    if deferred_at is not None and _utc(now) - _utc(deferred_at) >= DEFER_WAIT:
        return Verdict("take_over", owner, relation, timed_out=True)
    return Verdict("defer", owner, relation)


def skips(verdict: Verdict) -> bool:
    """이 판단이면 이 서버는 지금 그 PR의 일을 하지 않는다."""
    return verdict.action in ("elsewhere", "defer")


def should_recommend(verdict: Verdict) -> bool:
    """사람에게 알릴 판단: 검사하지 않거나 미룰 때, 판이 낮은 서버의 것을 넘겨받을 때."""
    return skips(verdict) or (verdict.action == "take_over" and verdict.relation < 0)
