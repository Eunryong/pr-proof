"""처리 기록 조회 (SQLite runs). 웹 쪽 동기화가 읽기만 하고, 쓰기는 워커가 한다."""

from pathlib import Path

from sqlalchemy.exc import OperationalError
from sqlmodel import Session, create_engine, select

from app.models.db import CommandRecord, Run
from app.models.sync_rules import RunSnapshot


class History:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.engine = create_engine(f"sqlite:///{db_path}")

    def runs_for(self, repo: str, pr_number: int, head_sha: str) -> list[RunSnapshot]:
        """같은 PR·head의 기록, 오래된 것부터. 워커가 한 번도 돌지 않아 DB가 없으면 빈 목록."""
        if not self.db_path.is_file():
            return []
        with Session(self.engine) as session:
            stmt = (
                select(Run)
                .where(Run.repo == repo, Run.pr_number == pr_number, Run.head_sha == head_sha)
                .order_by(Run.id)
            )
            return [
                RunSnapshot(state=r.state, heartbeat_at=r.heartbeat_at, finished_at=r.finished_at)
                for r in session.exec(stmt)
            ]

    def handled_command_ids(self, repo: str, pr_number: int) -> set[int]:
        """워커가 처리한 명령 코멘트 ID. 테이블이 아직 없으면 빈 집합."""
        if not self.db_path.is_file():
            return set()
        with Session(self.engine) as session:
            stmt = select(CommandRecord.comment_id).where(
                CommandRecord.repo == repo, CommandRecord.pr_number == pr_number
            )
            try:
                return set(session.exec(stmt))
            except OperationalError:
                return set()
