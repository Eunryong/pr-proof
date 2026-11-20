"""워커의 상태 읽기·쓰기: 처리 기록(SQLite), 최신 head 확인과 작업 키 정리(Redis)."""

from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from redis import Redis
from sqlalchemy import Engine, func, inspect, text
from sqlalchemy.schema import CreateColumn
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import state_keys
from app.models.call_budget import RulesetScope, Scope, Spent
from app.models.db import (
    AICall,
    CallGrant,
    CheckerEntry,
    CommandRecord,
    FixAttempt,
    ModelCacheEntry,
    ModelCall,
    Proposal,
    ProposalTarget,
    Record,
    RulesetEntry,
    Run,
    SeenViolation,
)
from app.models.job import PRJob
from app.models.records import NewRecord
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
        attempt_id: int | None = None,
    ) -> None:
        """열린 제안을 기록한다. 같은 스킬의 열린 제안이 있으면 그 기록을 새 커밋·번호로 바꾼다.

        targets는 이 수정안이 고친 위반이다(v1 경로). 기록을 바꿀 때는 이전 목록을 지우고 새로 쓴다.
        attempt_id는 올린 수정 시도(`fix_attempts`)다(문서 규칙 경로).
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
                    attempt_id=attempt_id,
                )  # fmt: skip
            else:
                row.head_sha, row.number, row.updated_at = head_sha, number, now
                row.attempt_id = attempt_id
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

    # --- 모델 호출 (ai_calls, model_cache) ---

    def add_model_call(self, call: ModelCall) -> int:
        with Session(self.engine) as session:
            session.add(call)
            session.commit()
            return call.id

    def finish_model_call(
        self,
        call_id: int,
        *,
        ok: bool,
        error: str | None,
        model_id: str,
        input_tokens: int | None,
        output_tokens: int | None,
        permission_denials: int,
    ) -> None:
        with Session(self.engine) as session:
            row = session.get(ModelCall, call_id)
            row.ok, row.error, row.model_id = ok, error, model_id
            row.input_tokens, row.output_tokens = input_tokens, output_tokens
            row.permission_denials = permission_denials
            session.add(row)
            session.commit()

    def calls_spent(self, repo: str, pr_number: int, scope: Scope, since: datetime) -> Spent:
        """상한에서 센 값. 규칙 묶음 상한은 그 묶음 키의 since 이후 호출을 PR과 무관하게, PR 상한은
        그 PR의 나머지 호출을 모든 커밋에 걸쳐, 하루 상한은 since 이후 저장소 전체를 센다.

        규칙 묶음 상한에 기간이 없으면 한 번 걸린 묶음은 상한을 올릴 때까지 영영 멈춘다. 기간을
        두면 다음 날의 검사가 남은 규칙부터 이어 한다 (채택한 검사기는 저장돼 있다)."""
        total = select(func.coalesce(func.sum(ModelCall.counted), 0)).where(ModelCall.repo == repo)
        if isinstance(scope, RulesetScope):
            in_scope = total.where(
                ModelCall.ruleset_key == scope.key, ModelCall.created_at >= since
            )
        else:
            in_scope = total.where(
                ModelCall.pr_number == pr_number, ModelCall.ruleset_key.is_(None)
            )
        with Session(self.engine) as session:
            return Spent(
                scope=int(session.exec(in_scope).one()),
                repo_daily=int(session.exec(total.where(ModelCall.created_at >= since)).one()),
            )

    def model_calls(self, repo: str, pr_number: int) -> list[ModelCall]:
        with Session(self.engine) as session:
            stmt = select(ModelCall).where(ModelCall.repo == repo, ModelCall.pr_number == pr_number)
            return list(session.exec(stmt.order_by(ModelCall.id)))

    def cached_result(self, key: str) -> ModelCacheEntry | None:
        with Session(self.engine) as session:
            return session.get(ModelCacheEntry, key)

    def save_result(self, key: str, *, stage: str, model: str, prompt_version: str,
                    result_json: str) -> None:  # fmt: skip
        """같은 키가 있으면 덮어쓴다. 같은 입력의 결과라 어느 쪽을 남겨도 된다."""
        entry = ModelCacheEntry(
            key=key, stage=stage, model=model, prompt_version=prompt_version,
            result_json=result_json, created_at=_now(),
        )  # fmt: skip
        with Session(self.engine) as session:
            session.merge(entry)
            session.commit()

    def saved_ruleset(self, key: str) -> str | None:
        with Session(self.engine) as session:
            row = session.get(RulesetEntry, key)
            return row.ruleset_json if row is not None else None

    def save_ruleset(self, key: str, ruleset_json: str) -> None:
        with Session(self.engine) as session:
            session.merge(RulesetEntry(key=key, ruleset_json=ruleset_json, created_at=_now()))
            session.commit()

    def saved_checker(self, key: str) -> CheckerEntry | None:
        with Session(self.engine) as session:
            return session.get(CheckerEntry, key)

    def save_checker(self, key: str, *, rule_id: str, accepted: bool, checker_json: str | None,
                     detail: str, attempts_json: str) -> None:  # fmt: skip
        entry = CheckerEntry(
            key=key, rule_id=rule_id, accepted=accepted, checker_json=checker_json, detail=detail,
            attempts_json=attempts_json, created_at=_now(),
        )  # fmt: skip
        with Session(self.engine) as session:
            session.merge(entry)
            session.commit()

    # --- 수정 시도, 위반, 과거 기록 (fix_attempts, violations, records) ---

    def save_fix_attempt(self, attempt: FixAttempt) -> int:
        attempt.created_at = _now()
        with Session(self.engine) as session:
            session.add(attempt)
            session.commit()
            return attempt.id

    def fix_attempt(self, attempt_id: int) -> FixAttempt | None:
        with Session(self.engine) as session:
            return session.get(FixAttempt, attempt_id)

    def replace_violations(
        self, repo: str, pr_number: int, head_sha: str, rows: Iterable[SeenViolation]
    ) -> None:
        """이 커밋의 위반 기록을 rows로 바꾼다. 같은 커밋을 다시 검사해도 한 벌만 남는다."""
        now = _now()
        with Session(self.engine) as session:
            stmt = select(SeenViolation).where(
                SeenViolation.repo == repo, SeenViolation.pr_number == pr_number,
                SeenViolation.head_sha == head_sha,
            )  # fmt: skip
            for old in session.exec(stmt):
                session.delete(old)
            for row in rows:
                row.created_at = now
                session.add(row)
            session.commit()

    def previous_violations(self, repo: str, pr_number: int, head_sha: str) -> list[SeenViolation]:
        """이 PR에서 head_sha가 아닌 커밋 중 가장 최근에 기록한 커밋의 위반."""
        with Session(self.engine) as session:
            last = session.exec(
                select(SeenViolation.head_sha)
                .where(SeenViolation.repo == repo, SeenViolation.pr_number == pr_number,
                       SeenViolation.head_sha != head_sha)
                .order_by(SeenViolation.id.desc())
            ).first()  # fmt: skip
            if last is None:
                return []
            stmt = select(SeenViolation).where(
                SeenViolation.repo == repo, SeenViolation.pr_number == pr_number,
                SeenViolation.head_sha == last,
            )  # fmt: skip
            return list(session.exec(stmt.order_by(SeenViolation.id)))

    def add_record(self, new: NewRecord) -> bool:
        """기록 하나를 넣는다. 같은 (repo, kind, source_id)가 이미 있으면 넣지 않고 False."""
        with Session(self.engine) as session:
            exists = session.exec(
                select(Record.id).where(
                    Record.repo == new.repo, Record.kind == new.kind,
                    Record.source_id == new.source_id,
                )
            ).first()  # fmt: skip
            if exists is not None:
                return False
            session.add(Record(**vars(new), created_at=_now()))
            session.commit()
            return True

    def records(self, repo: str, pr_number: int | None = None) -> list[Record]:
        with Session(self.engine) as session:
            stmt = select(Record).where(Record.repo == repo)
            if pr_number is not None:
                stmt = stmt.where(Record.pr_number == pr_number)
            return list(session.exec(stmt.order_by(Record.id)))

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

    def add_call_grant(self, repo: str, pr_number: int, head_sha: str, comment_id: int) -> None:
        """`/fix` 명령 하나의 PR 상한 추가. 같은 코멘트로 다시 불려도 하나다."""
        with Session(self.engine) as session:
            if session.get(CallGrant, comment_id) is None:
                session.add(CallGrant(comment_id=comment_id, repo=repo, pr_number=pr_number,
                                      head_sha=head_sha, created_at=_now()))  # fmt: skip
                session.commit()

    def call_grants(self, repo: str, pr_number: int) -> int:
        with Session(self.engine) as session:
            stmt = (
                select(func.count())
                .select_from(CallGrant)
                .where(CallGrant.repo == repo, CallGrant.pr_number == pr_number)
            )
            return int(session.exec(stmt).one())

    def last_comment_id(self, repo: str, pr_number: int) -> int | None:
        """이 PR에 단 코멘트 ID. 없으면 GitHub에서 찾는다 (worker/github.py)."""
        with Session(self.engine) as session:
            stmt = (
                select(Run.comment_id)
                .where(Run.repo == repo, Run.pr_number == pr_number, Run.comment_id.is_not(None))
                .order_by(Run.id.desc())
            )
            return session.exec(stmt).first()
