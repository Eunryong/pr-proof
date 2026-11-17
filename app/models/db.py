"""SQLite 테이블 정의. 연결과 쓰기는 각자 자기 쪽(워커는 worker/state.py)에서 한다."""

from datetime import datetime
from typing import Literal

from sqlmodel import Field, SQLModel

RunState = Literal["running", "done", "cancelled", "env_error"]


class Run(SQLModel, table=True):
    """PR·커밋별 검사 한 번."""

    id: int | None = Field(default=None, primary_key=True)
    repo: str = Field(index=True)
    pr_number: int = Field(index=True)
    head_sha: str = Field(index=True)
    kind: str
    source: str
    state: str  # RunState
    test_status: str | None = None  # PytestRun.status
    comment_id: int | None = None
    started_at: datetime
    heartbeat_at: datetime
    finished_at: datetime | None = None
