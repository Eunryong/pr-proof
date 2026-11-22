"""워커의 상태 읽기·쓰기: 처리 기록(SQLite), 최신 head 확인과 작업 키 정리(Redis)."""

from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from redis import Redis
from sqlalchemy import Engine, func, inspect, text
from sqlalchemy.schema import CreateColumn
from sqlmodel import Session, SQLModel, create_engine, select

from app.shared import state_keys_pure, version
from app.shared.db_pure import (
    Approval,
    CallGrant,
    CheckerEntry,
    CommandRecord,
    EvalRun,
    Feedback,
    FixAttempt,
    ModelCacheEntry,
    ModelCall,
    Proposal,
    RagAutoOff,
    Record,
    RulesetEntry,
    Run,
    SeenViolation,
)
from app.shared.job_pure import PRJob
from app.worker.access_pure import Author
from app.worker.call_budget_pure import RulesetScope, Scope, Spent
from app.worker.rag_switch_pure import COUNTED_KINDS, Evidence, FixOutcome, Outcomes, outcomes
from app.worker.records_pure import NewRecord


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


def open_engine(db_path: Path) -> Engine:
    """SQLite를 열고 새 테이블과 새 열을 만든다. 있던 줄은 그대로 둔다."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    add_missing_columns(engine)
    return engine


def add_eval_run(engine: Engine, run: EvalRun) -> int:
    with Session(engine) as session:
        session.add(run)
        session.commit()
        return run.id


def latest_baseline(
    engine: Engine, real_model: bool, before_id: int | None = None
) -> EvalRun | None:
    """가장 최근의 기준선. 가짜 모델의 실행과 실제 모델의 실행은 서로의 기준선이 아니다. before_id를
    주면 그보다 앞선 것."""
    with Session(engine) as session:
        stmt = select(EvalRun).where(EvalRun.baseline == True, EvalRun.real_model == real_model)  # noqa: E712
        if before_id is not None:
            stmt = stmt.where(EvalRun.id < before_id)
        return session.exec(stmt.order_by(EvalRun.id.desc())).first()


class Store:
    def __init__(self, db_path: Path, redis: Redis) -> None:
        self.engine = open_engine(db_path)
        self.redis = redis

    # --- Redis ---

    def is_stale(self, job: PRJob) -> bool:
        """이 작업의 커밋이 PR의 최신 head가 아니면 True. 기록이 없으면 낡지 않은 것으로 본다."""
        latest = self.redis.get(state_keys_pure.latest_head(job.repo, job.pr_number))
        return latest is not None and latest.decode() != job.head_sha

    def set_latest_head(self, repo: str, pr_number: int, head_sha: str) -> None:
        """웹훅 순서가 뒤집혀 낡은 head가 기록됐을 때 GitHub에서 확인한 값으로 바로잡는다."""
        self.redis.set(state_keys_pure.latest_head(repo, pr_number), head_sha)

    def release(self, job: PRJob) -> None:
        """작업 키를 지워 같은 작업을 다시 등록할 수 있게 한다."""
        self.redis.delete(state_keys_pure.job(job.dedup_key))

    # --- SQLite ---

    def start_run(
        self, job: PRJob, kind: str | None = None, *, author: Author | None = None
    ) -> int:
        """kind는 기본으로 작업 종류. /fix 재개는 "fix"로 남긴다. author는 워커가 GitHub에서 읽은
        PR 작성자이고, 작업에 실린 값과 함께 남긴다 (8.6)."""
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
            pipeline_commit=version.current() or None,
            author_association=author.association if author else None,
            author_login=author.login if author else None,
            job_author_association=job.author_association,
            job_author_login=job.author_login,
        )
        with Session(self.engine) as session:
            session.add(run)
            session.commit()
            return run.id

    def record_blocked(
        self, job: PRJob, head_sha: str, author: Author, comment_id: int | None
    ) -> None:
        """신뢰되지 않은 작성자의 PR이라 승인 안내만 달고 끝낸 작업 (8.6)."""
        run_id = self.start_run(job.model_copy(update={"head_sha": head_sha}), author=author)
        self.finish_run(run_id, "blocked", comment_id=comment_id)

    def add_approval(
        self, repo: str, pr_number: int, head_sha: str, approver: str, comment_id: int
    ) -> None:
        """`/recheck <커밋>` 하나의 승인. 같은 코멘트로 다시 불려도 하나다."""
        with Session(self.engine) as session:
            if session.get(Approval, comment_id) is None:
                session.add(Approval(comment_id=comment_id, repo=repo, pr_number=pr_number,
                                     head_sha=head_sha, approver=approver,
                                     created_at=_now()))  # fmt: skip
                session.commit()

    def approved_shas(self, repo: str, pr_number: int) -> set[str]:
        with Session(self.engine) as session:
            stmt = select(Approval.head_sha).where(
                Approval.repo == repo, Approval.pr_number == pr_number
            )
            return set(session.exec(stmt))

    def heartbeat(self, run_id: int) -> None:
        """runs 기록이 없는 작업(코멘트 수정, run_id 0)은 남길 줄이 없어 아무것도 하지 않는다."""
        with Session(self.engine) as session:
            run = session.get(Run, run_id)
            if run is None:
                return
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

    def runs(self, repo: str, pr_number: int, head_sha: str) -> list[Run]:
        """이 커밋의 검사 기록, 오래된 것부터."""
        with Session(self.engine) as session:
            stmt = select(Run).where(
                Run.repo == repo, Run.pr_number == pr_number, Run.head_sha == head_sha
            )
            return list(session.exec(stmt.order_by(Run.id)))

    # --- 제안 PR ---

    def record_proposal(
        self,
        repo: str,
        pr_number: int,
        skill: str,
        head_sha: str,
        branch: str,
        number: int,
        attempt_id: int | None = None,
    ) -> None:
        """열린 수정안을 기록한다. 같은 묶음의 열린 수정안이 있으면 그 기록을 새 커밋·번호로 바꾼다.

        attempt_id는 올린 수정 시도(`fix_attempts`)다. 머지·닫힘의 기록이 그 대상 위반으로 만든다.
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
            session.commit()

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
                     detail: str, attempts_json: str,
                     judge_route: str | None = None) -> None:  # fmt: skip
        entry = CheckerEntry(
            key=key, rule_id=rule_id, accepted=accepted, checker_json=checker_json, detail=detail,
            attempts_json=attempts_json, created_at=_now(), judge_route=judge_route,
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

    def violations_at(self, repo: str, pr_number: int, head_sha: str) -> list[SeenViolation]:
        """이 커밋의 규칙 검사가 찾은 위반. 아직 검사하지 않았으면 빈 목록이다."""
        with Session(self.engine) as session:
            stmt = select(SeenViolation).where(
                SeenViolation.repo == repo, SeenViolation.pr_number == pr_number,
                SeenViolation.head_sha == head_sha,
            )  # fmt: skip
            return list(session.exec(stmt.order_by(SeenViolation.id)))

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

    def records_by_ids(self, ids: Iterable[int]) -> list[Record]:
        wanted = list(dict.fromkeys(ids))
        if not wanted:
            return []
        with Session(self.engine) as session:
            return list(session.exec(select(Record).where(Record.id.in_(wanted))))

    def pending_records(self, repo: str) -> list[Record]:
        """벡터 저장소에 아직 넣지 않은 이 저장소의 기록 (`embedded_at`이 빔, 6.5)."""
        with Session(self.engine) as session:
            stmt = select(Record).where(Record.repo == repo, Record.embedded_at.is_(None))
            return list(session.exec(stmt.order_by(Record.id)))

    def mark_embedded(self, ids: Iterable[int], model: str, at: datetime) -> None:
        with Session(self.engine) as session:
            for row in session.exec(select(Record).where(Record.id.in_(list(ids)))):
                row.embedded_at, row.embed_model = at, model
                session.add(row)
            session.commit()

    def clear_embedded(self, repo: str) -> None:
        with Session(self.engine) as session:
            for row in session.exec(select(Record).where(Record.repo == repo)):
                row.embedded_at, row.embed_model = None, None
                session.add(row)
            session.commit()

    def record_repos(self) -> list[str]:
        with Session(self.engine) as session:
            return sorted(set(session.exec(select(Record.repo))))

    def records(self, repo: str, pr_number: int | None = None) -> list[Record]:
        with Session(self.engine) as session:
            stmt = select(Record).where(Record.repo == repo)
            if pr_number is not None:
                stmt = stmt.where(Record.pr_number == pr_number)
            return list(session.exec(stmt.order_by(Record.id)))

    # --- 검색을 켜는 조건 (6.7) ---

    def rag_evidence(self, repo: str) -> Evidence:
        """auto가 보는 이 저장소의 숫자. 채택·거절 기록 수와 결과를 아는 수정 시도이고, 어느 시도를
        세는지는 `rag_switch.outcomes`가 정한다."""
        with Session(self.engine) as session:
            records = session.exec(
                select(func.count()).select_from(Record).where(
                    Record.repo == repo, Record.kind.in_(sorted(COUNTED_KINDS))
                )
            ).one()  # fmt: skip
            rows = session.exec(
                select(FixAttempt.id, FixAttempt.bundle, Proposal.state, FixAttempt.references_json)
                .join(FixAttempt, FixAttempt.id == Proposal.attempt_id)
                .where(Proposal.repo == repo, FixAttempt.repo == repo)
            ).all()  # fmt: skip
        with_refs, without_refs = outcomes(FixOutcome(*row) for row in rows)
        return Evidence(int(records), with_refs, without_refs)

    def rag_turned_off(self, repo: str, min_attempts: int, min_baseline: int) -> Evidence | None:
        """auto가 같은 비교 기준값으로 이 저장소의 검색을 끈 때의 숫자. 없으면 None."""
        with Session(self.engine) as session:
            row = session.get(RagAutoOff, repo)
        if row is None or (row.min_attempts, row.min_baseline) != (min_attempts, min_baseline):
            return None
        return Evidence(
            row.records, Outcomes(row.with_adopted, row.with_known),
            Outcomes(row.without_adopted, row.without_known),
        )  # fmt: skip

    def turn_rag_off(
        self, repo: str, evidence: Evidence, min_attempts: int, min_baseline: int
    ) -> None:
        """auto가 검색을 끈 사실을 남긴다. 다시 불려도 저장소마다 한 줄이다."""
        with Session(self.engine) as session:
            session.merge(RagAutoOff(
                repo=repo, min_attempts=min_attempts, min_baseline=min_baseline,
                records=evidence.records,
                with_adopted=evidence.with_refs.adopted, with_known=evidence.with_refs.known,
                without_adopted=evidence.without_refs.adopted,
                without_known=evidence.without_refs.known, created_at=_now(),
            ))  # fmt: skip
            session.commit()

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

    # --- PR 코멘트 (feedback) ---

    def record_feedback(
        self,
        *,
        repo: str,
        pr_number: int,
        kind: str,
        item_id: int,
        thread_id: str | None,
        login: str,
        head_sha: str,
        verdict: str,
        why: str = "",
        reply_id: int | None = None,
        proposal_number: int | None = None,
        resolved_at: datetime | None = None,
    ) -> None:
        """처리한 코멘트를 적는다. 같은 (repo, kind, item_id)가 있으면 그 줄을 고친다.

        resolved_at은 처리하면서 스레드를 바로 해결됨으로 바꾼 때다(이미 고쳐진 코멘트, 4.11)."""
        with Session(self.engine) as session:
            stmt = select(Feedback).where(
                Feedback.repo == repo, Feedback.kind == kind, Feedback.item_id == item_id
            )
            row = session.exec(stmt).first()
            if row is None:
                row = Feedback(
                    repo=repo, pr_number=pr_number, kind=kind, item_id=item_id, login=login,
                    head_sha=head_sha, verdict=verdict, handled_at=_now(),
                )  # fmt: skip
            row.pr_number, row.thread_id, row.login, row.head_sha = (
                pr_number, thread_id, login, head_sha,
            )  # fmt: skip
            row.verdict, row.why, row.reply_id = verdict, why, reply_id
            row.proposal_number, row.handled_at = proposal_number, _now()
            row.resolved_at = resolved_at
            session.add(row)
            session.commit()

    def handled_feedback(self, repo: str, pr_number: int) -> list[Feedback]:
        with Session(self.engine) as session:
            stmt = select(Feedback).where(Feedback.repo == repo, Feedback.pr_number == pr_number)
            return list(session.exec(stmt.order_by(Feedback.id)))

    def feedback_awaiting_merge(self, repo: str, pr_number: int) -> list[Feedback]:
        """받아들여 수정안을 올렸는데 스레드를 아직 해결하지 않은 줄 코멘트."""
        with Session(self.engine) as session:
            stmt = select(Feedback).where(
                Feedback.repo == repo, Feedback.pr_number == pr_number,
                Feedback.verdict == "accepted",
                Feedback.proposal_number.is_not(None),
                Feedback.resolved_at.is_(None),
                Feedback.thread_id.is_not(None),
            )  # fmt: skip
            return list(session.exec(stmt.order_by(Feedback.id)))

    def mark_feedback_resolved(self, ids: Iterable[int], at: datetime) -> None:
        with Session(self.engine) as session:
            for feedback_id in ids:
                row = session.get(Feedback, feedback_id)
                if row is not None:
                    row.resolved_at = at
                    session.add(row)
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
        """이 PR에 단 코멘트 ID. 없으면 GitHub에서 찾는다 (app/worker/github.py)."""
        with Session(self.engine) as session:
            stmt = (
                select(Run.comment_id)
                .where(Run.repo == repo, Run.pr_number == pr_number, Run.comment_id.is_not(None))
                .order_by(Run.id.desc())
            )
            return session.exec(stmt).first()
