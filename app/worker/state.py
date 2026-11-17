"""워커의 상태 읽기·쓰기: 처리 기록(SQLite), 최신 head 확인과 작업 키 정리(Redis)."""

from datetime import UTC, datetime
from pathlib import Path

from redis import Redis
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import state_keys
from app.models.db import Run
from app.models.job import PRJob


def _now() -> datetime:
    return datetime.now(UTC)


class Store:
    def __init__(self, db_path: Path, redis: Redis) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(f"sqlite:///{db_path}")
        SQLModel.metadata.create_all(self.engine)
        self.redis = redis

    # --- Redis ---

    def is_stale(self, job: PRJob) -> bool:
        """이 작업의 커밋이 PR의 최신 head가 아니면 True. 기록이 없으면 낡지 않은 것으로 본다."""
        latest = self.redis.get(state_keys.latest_head(job.repo, job.pr_number))
        return latest is not None and latest.decode() != job.head_sha

    def set_latest_head(self, repo: str, pr_number: int, head_sha: str) -> None:
        """웹훅 순서가 뒤집혀 낡은 head가 기록됐을 때 GitHub에서 확인한 값으로 바로잡는다."""
        self.redis.set(state_keys.latest_head(repo, pr_number), head_sha)

    def release(self, job: PRJob) -> None:
        """작업 키를 지워 같은 작업을 다시 등록할 수 있게 한다."""
        self.redis.delete(state_keys.job(job.dedup_key))

    # --- SQLite ---

    def start_run(self, job: PRJob) -> int:
        now = _now()
        run = Run(
            repo=job.repo,
            pr_number=job.pr_number,
            head_sha=job.head_sha or "",
            kind=job.kind,
            source=job.source,
            state="running",
            started_at=now,
            heartbeat_at=now,
        )
        with Session(self.engine) as session:
            session.add(run)
            session.commit()
            return run.id

    def heartbeat(self, run_id: int) -> None:
        with Session(self.engine) as session:
            run = session.get(Run, run_id)
            run.heartbeat_at = _now()
            session.commit()

    def finish_run(
        self,
        run_id: int,
        state: str,
        *,
        test_status: str | None = None,
        comment_id: int | None = None,
    ) -> None:
        with Session(self.engine) as session:
            run = session.get(Run, run_id)
            run.state = state
            run.test_status = test_status
            run.comment_id = comment_id if comment_id is not None else run.comment_id
            run.finished_at = _now()
            run.heartbeat_at = run.finished_at
            session.commit()

    def last_comment_id(self, repo: str, pr_number: int) -> int | None:
        """이 PR에 단 코멘트 ID. 없으면 GitHub에서 찾는다 (worker/github.py)."""
        with Session(self.engine) as session:
            stmt = (
                select(Run.comment_id)
                .where(Run.repo == repo, Run.pr_number == pr_number, Run.comment_id.is_not(None))
                .order_by(Run.id.desc())
            )
            return session.exec(stmt).first()
