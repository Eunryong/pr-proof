"""워커의 상태 읽기·쓰기: 처리 기록(SQLite), 최신 head 확인과 작업 키 정리(Redis)."""

from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from redis import Redis
from sqlalchemy import Engine, inspect, text
from sqlalchemy.schema import CreateColumn
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import state_keys
from app.models.db import AICall, CommandRecord, Proposal, ProposalTarget, Run
from app.models.job import PRJob
from app.models.rejection import RejectionKey


def _now() -> datetime:
    return datetime.now(UTC)


def add_missing_columns(engine: Engine) -> None:
    """모델에 더한 열을 이미 있는 테이블에 더한다. create_all은 새 테이블만 만든다.

    더할 수 있는 것은 NULL을 허용하는 열뿐이다. 그 밖의 변경은 여기서 다루지 않는다.
    """
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                if not column.nullable:
                    raise RuntimeError(f"{table.name}.{column.name}: NULL 불가 열은 더할 수 없다")
                ddl = CreateColumn(column).compile(engine)
                conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {ddl}"))


class Store:
    def __init__(self, db_path: Path, redis: Redis) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(f"sqlite:///{db_path}")
        SQLModel.metadata.create_all(self.engine)
        add_missing_columns(self.engine)
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

    def start_run(self, job: PRJob, kind: str | None = None) -> int:
        """kind는 기본으로 작업 종류. /fix 재개는 "fix"로 남긴다."""
        now = _now()
        run = Run(
            repo=job.repo,
            pr_number=job.pr_number,
            head_sha=job.head_sha or "",
            kind=kind or job.kind,
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

    # --- 제안 PR ---

    def record_proposal(
        self,
        repo: str,
        pr_number: int,
        skill: str,
        head_sha: str,
        branch: str,
        number: int,
        targets: Iterable[RejectionKey] = (),
    ) -> None:
        """열린 제안을 기록한다. 같은 스킬의 열린 제안이 있으면 그 기록을 새 커밋·번호로 바꾼다.

        targets는 이 수정안이 고친 위반이다. 기록을 바꿀 때는 이전 목록을 지우고 새로 쓴다.
        """
        now = _now()
        with Session(self.engine) as session:
            stmt = select(Proposal).where(
                Proposal.repo == repo, Proposal.pr_number == pr_number,
                Proposal.skill == skill, Proposal.state == "open",
            )  # fmt: skip
            row = session.exec(stmt).first()
            if row is None:
                row = Proposal(
                    repo=repo, pr_number=pr_number, skill=skill, head_sha=head_sha,
                    branch=branch, number=number, state="open", created_at=now, updated_at=now,
                )  # fmt: skip
            else:
                row.head_sha, row.number, row.updated_at = head_sha, number, now
            session.add(row)
            session.flush()
            old = select(ProposalTarget).where(ProposalTarget.proposal_id == row.id)
            for target in session.exec(old):
                session.delete(target)
            for file, code, line_text in dict.fromkeys(targets):
                session.add(
                    ProposalTarget(proposal_id=row.id, file=file, code=code, line_text=line_text)
                )
            session.commit()

    def rejected_targets(self, repo: str, pr_number: int) -> dict[RejectionKey, int]:
        """이 PR에서 사람이 닫은 제안이 고친 위반 → 그 제안 PR 번호 (같은 위반이면 최근 것)."""
        with Session(self.engine) as session:
            stmt = (
                select(ProposalTarget, Proposal.number)
                .join(Proposal, ProposalTarget.proposal_id == Proposal.id)
                .where(
                    Proposal.repo == repo,
                    Proposal.pr_number == pr_number,
                    Proposal.state == "rejected",
                )  # fmt: skip
                .order_by(Proposal.id)
            )
            return {(t.file, t.code, t.line_text): number for t, number in session.exec(stmt)}

    def proposals(self, repo: str, pr_number: int, state: str | None = None) -> list[Proposal]:
        with Session(self.engine) as session:
            stmt = select(Proposal).where(Proposal.repo == repo, Proposal.pr_number == pr_number)
            if state is not None:
                stmt = stmt.where(Proposal.state == state)
            return list(session.exec(stmt.order_by(Proposal.id)))

    def set_proposal_state(
        self, proposal_id: int, state: str, merge_commit: str | None = None
    ) -> None:
        with Session(self.engine) as session:
            row = session.get(Proposal, proposal_id)
            row.state, row.updated_at = state, _now()
            if merge_commit is not None:
                row.merge_commit = merge_commit
            session.add(row)
            session.commit()

    # --- AI 비용 ---

    def record_ai_call(self, call: AICall) -> None:
        call.created_at = _now()
        with Session(self.engine) as session:
            session.add(call)
            session.commit()

    def ai_spent(self, repo: str, pr_number: int) -> float:
        """이 PR에 지금까지 쓴 AI 비용 (예산에서 뺀 값의 합)."""
        with Session(self.engine) as session:
            stmt = select(AICall.charged_usd).where(
                AICall.repo == repo, AICall.pr_number == pr_number
            )
            return float(sum(session.exec(stmt).all()))

    def command_handled(self, comment_id: int) -> bool:
        with Session(self.engine) as session:
            return session.get(CommandRecord, comment_id) is not None

    def record_command(self, job: PRJob, result: str) -> None:
        with Session(self.engine) as session:
            session.merge(
                CommandRecord(
                    comment_id=job.comment_id,
                    repo=job.repo,
                    pr_number=job.pr_number,
                    command=job.command or "",
                    result=result,
                    handled_at=_now(),
                )
            )
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
