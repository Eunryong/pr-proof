"""SQLite 테이블 정의. 연결과 쓰기는 각자 자기 쪽(워커는 worker/state.py)에서 한다."""

from datetime import datetime
from typing import Literal

from sqlmodel import Field, SQLModel

# error: 환경 오류가 아닌 예외(파이프라인 버그, 작업 시간 초과). 같은 커밋은 다시 넣지 않는다
RunState = Literal["running", "done", "cancelled", "env_error", "error"]


# open: 열려 있음, merged: 머지됨, superseded: 원본 PR에 새 커밋이 와서 파이프라인이 닫음,
# rejected: 사람이 머지하지 않고 닫음, closed_with_pr: 원본 PR이 닫혀 파이프라인이 닫음
ProposalState = Literal["open", "merged", "superseded", "rejected", "closed_with_pr"]


class Proposal(SQLModel, table=True):
    """제안 PR (DESIGN.md F10). 머지 여부는 커밋 메시지가 아니라 이 번호로 GitHub에서 확인한다."""

    id: int | None = Field(default=None, primary_key=True)
    repo: str = Field(index=True)
    pr_number: int = Field(index=True)  # 원본 PR
    skill: str
    head_sha: str  # 수정안을 만든 원본 PR의 커밋
    branch: str
    number: int  # 제안 PR 번호
    state: str  # ProposalState
    merge_commit: str | None = None
    created_at: datetime
    updated_at: datetime


class CommandRecord(SQLModel, table=True):
    """처리한 명령 코멘트. 같은 코멘트에 두 번 답하지 않는다 (DESIGN.md F11)."""

    comment_id: int = Field(primary_key=True)
    repo: str = Field(index=True)
    pr_number: int
    command: str
    result: str
    handled_at: datetime


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
