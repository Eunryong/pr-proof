"""동기화가 같은 head를 다시 넣을지 정하는 규칙 (docs/DESIGN.md F2 "끝난 상태별 재등록 규칙").

재시도 횟수는 작업 하나 안에서만 세므로, 동기화가 runs의 끝난 상태를 보고 다시 넣을지 정해야
환경 오류가 끝없이 반복되지 않는다.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from app.shared.other_server_pure import DEFER_WAIT, DEFERRED, ELSEWHERE

EnqueueReason = Literal[
    "no_record", "env_error_backoff", "dead_worker", "cancelled", "new_pipeline", "deferred"
]


@dataclass(frozen=True)
class RunSnapshot:
    state: str  # app/shared/db_pure.RunState
    heartbeat_at: datetime
    finished_at: datetime | None
    pipeline_commit: str | None = None  # 그 검사를 돌린 파이프라인 코드의 커밋. 옛 기록은 None


def _utc(value: datetime) -> datetime:
    """SQLite는 시간대를 저장하지 않으므로 시간대 없는 값은 UTC로 본다."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def enqueue_reason(
    runs: Sequence[RunSnapshot],
    now: datetime,
    *,
    env_error_backoff: timedelta,
    heartbeat_timeout: timedelta,
    pipeline_commit: str = "",
) -> EnqueueReason | None:
    """같은 PR·head의 runs(오래된 것부터)를 보고, 다시 넣을 이유를 돌려준다. 넣지 않으면 None.

    pipeline_commit은 지금 도는 파이프라인 코드의 커밋이다. 끝난 결과(done, error)가 모두 다른
    커밋의 코드가 낸 것이면 `/recheck` 없이 다시 검사한다(배포 뒤 열린 PR의 결과가 옛 코드의 것으로
    남지 않게). 커밋을 모르면(빈 문자열) 이 규칙을 쓰지 않는다. 지금 코드로 한 번 끝나면 그 기록이
    남아 다시 넣지 않는다.
    """
    if not runs:
        return "no_record"
    last = runs[-1]
    settled = [run for run in runs if run.state in ("done", "error")]
    by_old_code = bool(pipeline_commit) and all(
        run.pipeline_commit != pipeline_commit for run in settled
    )
    stale = bool(settled) and by_old_code  # 끝난 결과가 있지만 모두 옛 코드의 것이다
    if stale and last.state in ("done", "error"):
        return "new_pipeline"
    if not stale and any(run.state == "done" for run in runs):
        return None  # 결과가 이미 있다. 새 커밋이 올 때만 검사한다
    if last.state == "blocked":
        # 신뢰되지 않은 작성자의 PR이라 승인을 기다린다 (8.6). 승인(`/recheck <커밋>`)이나 새 커밋이
        # 와야 다시 검사한다. 주기마다 다시 넣지 않는다
        return None
    if last.state == "error":
        # 파이프라인 버그나 시간 초과는 같은 커밋으로 다시 돌려도 같은 결과다. 새 커밋 때만 검사한다
        return None
    if last.state == ELSEWHERE:
        # 같은 저장소를 보는 다른 서버가 이 커밋을 이미 처리했다. 새 커밋 때만 다시 본다
        return None
    if last.state == DEFERRED:
        # 다른 서버가 이 커밋을 처리하기를 기다린다. 시간이 지나면 한 번 더 넣어, 워커가 그 서버가
        # 처리했는지 보고 아니면 넘겨받게 한다
        finished = _utc(last.finished_at or last.heartbeat_at)
        return "deferred" if now - finished >= DEFER_WAIT else None
    if last.state == "env_error":
        finished = _utc(last.finished_at or last.heartbeat_at)
        return "env_error_backoff" if now - finished >= env_error_backoff else None
    if last.state == "running":
        return "dead_worker" if now - _utc(last.heartbeat_at) >= heartbeat_timeout else None
    if last.state == "cancelled":
        return "cancelled"  # 이 head가 다시 최신인데 결과가 없다
    return None
