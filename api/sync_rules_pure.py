"""동기화가 같은 head를 다시 넣을지 정하는 규칙 (docs/DESIGN.md F2 "끝난 상태별 재등록 규칙").

재시도 횟수는 작업 하나 안에서만 세므로, 동기화가 runs의 끝난 상태를 보고 다시 넣을지 정해야
환경 오류가 끝없이 반복되지 않는다.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

EnqueueReason = Literal["no_record", "env_error_backoff", "dead_worker", "cancelled"]


@dataclass(frozen=True)
class RunSnapshot:
    state: str  # shared/db_pure.RunState
    heartbeat_at: datetime
    finished_at: datetime | None


def _utc(value: datetime) -> datetime:
    """SQLite는 시간대를 저장하지 않으므로 시간대 없는 값은 UTC로 본다."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def enqueue_reason(
    runs: Sequence[RunSnapshot],
    now: datetime,
    *,
    env_error_backoff: timedelta,
    heartbeat_timeout: timedelta,
) -> EnqueueReason | None:
    """같은 PR·head의 runs(오래된 것부터)를 보고, 다시 넣을 이유를 돌려준다. 넣지 않으면 None."""
    if not runs:
        return "no_record"
    if any(run.state == "done" for run in runs):
        return None  # 결과가 이미 있다. 새 커밋이 올 때만 검사한다
    last = runs[-1]
    if last.state == "blocked":
        # 신뢰되지 않은 작성자의 PR이라 승인을 기다린다 (8.6). 승인(`/recheck <커밋>`)이나 새 커밋이
        # 와야 다시 검사한다. 주기마다 다시 넣지 않는다
        return None
    if last.state == "error":
        # 파이프라인 버그나 시간 초과는 같은 커밋으로 다시 돌려도 같은 결과다. 새 커밋 때만 검사한다
        return None
    if last.state == "env_error":
        finished = _utc(last.finished_at or last.heartbeat_at)
        return "env_error_backoff" if now - finished >= env_error_backoff else None
    if last.state == "running":
        return "dead_worker" if now - _utc(last.heartbeat_at) >= heartbeat_timeout else None
    if last.state == "cancelled":
        return "cancelled"  # 이 head가 다시 최신인데 결과가 없다
    return None
