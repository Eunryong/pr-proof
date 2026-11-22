"""처리 기록 조회 (SQLite runs). 웹 쪽 동기화가 읽기만 하고, 쓰기는 워커가 한다."""

from pathlib import Path
from typing import Final

from sqlalchemy.exc import OperationalError
from sqlmodel import Session, create_engine, select

from app.api.sync_rules_pure import RunSnapshot
from app.shared.db_pure import CommandRecord, Feedback, Proposal, Run

# 머지되지 않고 닫힌 수정안의 상태 (db_pure.ProposalState)
CLOSED_UNMERGED: Final = ("rejected", "superseded", "closed_with_pr")


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
                RunSnapshot(
                    state=r.state,
                    heartbeat_at=r.heartbeat_at,
                    finished_at=r.finished_at,
                    pipeline_commit=r.pipeline_commit,
                )
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

    def prs_with_open_proposals(self, repo: str) -> set[int]:
        """열린 제안 PR이 남아 있는 원본 PR 번호.

        원본이 닫혔는데 close 웹훅을 놓쳤는지 볼 때 쓴다.
        """
        if not self.db_path.is_file():
            return set()
        with Session(self.engine) as session:
            stmt = select(Proposal.pr_number).where(Proposal.repo == repo, Proposal.state == "open")
            try:
                return set(session.exec(stmt))
            except OperationalError:
                return set()

    def handled_feedback_keys(self, repo: str, pr_number: int) -> set[tuple[str, int]]:
        """워커가 처리한 코멘트의 (종류, id). 테이블이 아직 없으면 빈 집합."""
        if not self.db_path.is_file():
            return set()
        with Session(self.engine) as session:
            stmt = select(Feedback.kind, Feedback.item_id).where(
                Feedback.repo == repo, Feedback.pr_number == pr_number
            )
            try:
                return {(kind, item_id) for kind, item_id in session.exec(stmt)}
            except OperationalError:
                return set()

    def prs_awaiting_resolve(self, repo: str) -> set[int]:
        """받아들인 코멘트의 스레드를 아직 해결하지 않은 원래 PR 번호.

        수정안이 머지됐는데 머지 웹훅을 놓쳤는지 볼 때 쓴다. 머지됐는지는 워커가 GitHub에서 본다.
        머지되지 않고 닫힌 것으로 기록된 수정안의 코멘트는 뺀다(워커가 스레드를 바꾸지 않으므로
        주기마다 다시 넣어 봐야 일이 없다).
        """
        if not self.db_path.is_file():
            return set()
        with Session(self.engine) as session:
            stmt = select(Feedback.pr_number).where(
                Feedback.repo == repo,
                Feedback.verdict == "accepted",
                Feedback.proposal_number.is_not(None),
                Feedback.resolved_at.is_(None),
                Feedback.thread_id.is_not(None),
                ~select(Proposal.id)
                .where(
                    Proposal.repo == Feedback.repo,
                    Proposal.number == Feedback.proposal_number,
                    Proposal.state.in_(CLOSED_UNMERGED),
                )
                .exists(),
            )
            try:
                return set(session.exec(stmt))
            except OperationalError:
                return set()
