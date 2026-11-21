from pathlib import Path

import fakeredis
import pytest
from sqlalchemy import create_engine, inspect, text

from shared import state_keys_pure as state_keys
from shared.db_pure import FixAttempt, SeenViolation
from shared.job_pure import PRJob
from worker.access_pure import Author
from worker.rag_switch_pure import Evidence, Outcomes
from worker.records_pure import NewRecord
from worker.state import Store

from .rag_helpers import fix_outcome, record


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "db" / "pipeline.db", fakeredis.FakeStrictRedis())


def _job(head: str = "abc") -> PRJob:
    return PRJob(
        repo="o/r", pr_number=1, kind="update", head_sha=head, base_sha="b", source="webhook"
    )


def test_is_stale_compares_latest_head(store: Store) -> None:
    assert not store.is_stale(_job())  # 기록 없음
    store.redis.set(state_keys.latest_head("o/r", 1), "abc")
    assert not store.is_stale(_job("abc"))
    store.redis.set(state_keys.latest_head("o/r", 1), "def")
    assert store.is_stale(_job("abc"))


def test_release_removes_job_key(store: Store) -> None:
    job = _job()
    store.redis.set(state_keys.job(job.dedup_key), "1")
    store.release(job)
    assert store.redis.get(state_keys.job(job.dedup_key)) is None


def test_run_lifecycle_and_last_comment(store: Store) -> None:
    assert store.last_comment_id("o/r", 1) is None
    first = store.start_run(_job())
    store.heartbeat(first)
    store.finish_run(first, "done", test_status="passed", comment_id=7)
    second = store.start_run(_job("def"))
    store.finish_run(second, "cancelled")  # 코멘트 ID 없이 끝나도
    assert store.last_comment_id("o/r", 1) == 7  # 마지막으로 단 코멘트를 기억한다


def test_reproposal_of_the_same_bundle_updates_the_open_record(store: Store) -> None:
    store.record_proposal("o/r", 1, "mechanical", "h1", "b", 900, attempt_id=1)
    store.record_proposal("o/r", 1, "mechanical", "h2", "b", 902, attempt_id=2)
    store.record_proposal("o/r", 1, "model", "h2", "m", 903, attempt_id=3)
    rows = store.proposals("o/r", 1)
    assert [(r.skill, r.head_sha, r.number, r.attempt_id) for r in rows] == [
        ("mechanical", "h2", 902, 2),
        ("model", "h2", 903, 3),
    ]


def test_old_v1_proposal_targets_table_is_left_alone(tmp_path: Path) -> None:
    """3단계에 v1 lint의 수정안 기록(`proposaltarget`)을 쓰지 않게 되었다. 그 테이블이 남은
    데이터베이스도 그대로 열리고, 그 테이블과 행은 지우지 않는다."""
    path = tmp_path / "pipeline.db"
    old = create_engine(f"sqlite:///{path}")
    with old.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE proposaltarget (id INTEGER PRIMARY KEY, proposal_id INTEGER NOT NULL,"
                " file VARCHAR NOT NULL, code VARCHAR NOT NULL, line_text VARCHAR NOT NULL)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO proposaltarget (proposal_id, file, code, line_text) "
                "VALUES (1, 'a.py', 'F401', 'import os')"
            )
        )
    old.dispose()
    store = Store(path, fakeredis.FakeStrictRedis())
    with store.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM proposaltarget")).scalar() == 1
    store.record_proposal("o/r", 1, "mechanical", "h1", "b", 900)
    assert [p.number for p in store.proposals("o/r", 1)] == [900]


def test_old_dollar_table_is_left_alone(tmp_path: Path) -> None:
    """2단계에 v1 static의 달러 기록(`aicall`)을 없앴다. 그 테이블이 남은 데이터베이스도 그대로
    열리고, 그 테이블과 행은 지우지 않는다."""
    path = tmp_path / "pipeline.db"
    old = create_engine(f"sqlite:///{path}")
    with old.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE aicall (id INTEGER PRIMARY KEY, repo VARCHAR NOT NULL, "
                "pr_number INTEGER NOT NULL, charged_usd FLOAT NOT NULL)"
            )
        )
        conn.execute(
            text("INSERT INTO aicall (repo, pr_number, charged_usd) VALUES ('o/r', 1, 0.1)")
        )
    old.dispose()

    store = Store(path, fakeredis.FakeStrictRedis())
    Store(path, fakeredis.FakeStrictRedis())  # 다시 열어도 같은 상태
    with store.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM aicall")).scalar() == 1
    store.record_proposal("o/r", 1, "mechanical", "h1", "b", 900)
    assert [p.number for p in store.proposals("o/r", 1)] == [900]


def test_existing_proposal_table_gets_the_attempt_column(tmp_path: Path) -> None:
    """2단계 전의 `proposal` 테이블에 attempt_id가 더해지고 옛 행은 그대로 읽힌다. 새 테이블
    (fix_attempts, violations, records)은 새로 생긴다."""
    path = tmp_path / "pipeline.db"
    old = create_engine(f"sqlite:///{path}")
    with old.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE proposal (id INTEGER PRIMARY KEY, repo VARCHAR NOT NULL, "
                "pr_number INTEGER NOT NULL, skill VARCHAR NOT NULL, head_sha VARCHAR NOT NULL, "
                "branch VARCHAR NOT NULL, number INTEGER NOT NULL, state VARCHAR NOT NULL, "
                "merge_commit VARCHAR, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO proposal (repo, pr_number, skill, head_sha, branch, number, state, "
                "created_at, updated_at) VALUES ('o/r', 1, 'lint', 'h', 'b', 900, 'open', "
                "'2026-11-01 00:00:00', '2026-11-01 00:00:00')"
            )
        )
    old.dispose()

    store = Store(path, fakeredis.FakeStrictRedis())
    tables = set(inspect(store.engine).get_table_names())
    assert {"fix_attempts", "violations", "records"} <= tables
    (row,) = store.proposals("o/r", 1)
    assert row.number == 900 and row.attempt_id is None
    store.record_proposal("o/r", 1, "model", "h", "pipeline/pr-1-fix-model", 901, attempt_id=7)
    assert [p.attempt_id for p in store.proposals("o/r", 1)] == [None, 7]


def _record(source_id: str = "fix_pr:900:v1", kind: str = "rejected") -> NewRecord:
    return NewRecord(
        repo="o/r", kind=kind, origin="fix_pr", source_id=source_id, rule_id="R",
        rule_text="t", file="a.py", line_text="print(x)", context="", diff="", pr_number=1,
        fix_pr_number=900,
    )  # fmt: skip


def test_same_record_is_stored_once(store: Store) -> None:
    """기록의 고유 키는 (repo, kind, source_id)다 (4.10). 같은 일을 다시 남겨도 하나다."""
    assert store.add_record(_record())
    assert not store.add_record(_record())
    assert store.add_record(_record(kind="adopted"))
    assert [r.kind for r in store.records("o/r", 1)] == ["rejected", "adopted"]
    assert store.records("o/r", 2) == []


def test_violations_of_a_commit_are_replaced_and_the_previous_commit_is_found(store) -> None:
    def row(head: str, vid: str) -> SeenViolation:
        return SeenViolation(repo="o/r", pr_number=1, head_sha=head, violation_id=vid,
                             rule_id="R", file="a.py", line=2, line_text="x", message="m",
                             unit="line")  # fmt: skip

    assert store.previous_violations("o/r", 1, "h1") == []
    store.replace_violations("o/r", 1, "h1", [row("h1", "a"), row("h1", "b")])
    store.replace_violations("o/r", 1, "h1", [row("h1", "c")])  # 같은 커밋을 다시 검사
    store.replace_violations("o/r", 1, "h2", [row("h2", "d")])
    assert [v.violation_id for v in store.previous_violations("o/r", 1, "h2")] == ["c"]
    assert [v.violation_id for v in store.previous_violations("o/r", 1, "h3")] == ["d"]


def test_existing_runs_table_gets_the_author_columns_and_approvals_is_created(
    tmp_path: Path,
) -> None:
    """6단계 전의 `run` 테이블(작성자 열 없음)이 그대로 열리고 옛 행이 읽힌다. `approvals`는
    새로 생긴다. 동기화가 읽는 옛 행의 상태도 그대로다."""
    path = tmp_path / "pipeline.db"
    old = create_engine(f"sqlite:///{path}")
    with old.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE run (id INTEGER PRIMARY KEY, repo VARCHAR NOT NULL, "
                "pr_number INTEGER NOT NULL, head_sha VARCHAR NOT NULL, kind VARCHAR NOT NULL, "
                "source VARCHAR NOT NULL, state VARCHAR NOT NULL, test_status VARCHAR, "
                "comment_id INTEGER, started_at DATETIME NOT NULL, "
                "heartbeat_at DATETIME NOT NULL, finished_at DATETIME)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO run (repo, pr_number, head_sha, kind, source, state, comment_id, "
                "started_at, heartbeat_at) VALUES ('o/r', 1, 'h', 'update', 'webhook', 'done', "
                "555, '2026-11-01 00:00:00', '2026-11-01 00:00:00')"
            )
        )
    old.dispose()

    store = Store(path, fakeredis.FakeStrictRedis())
    Store(path, fakeredis.FakeStrictRedis())  # 다시 열어도 같은 상태
    assert "approvals" in set(inspect(store.engine).get_table_names())
    assert store.last_comment_id("o/r", 1) == 555
    with store.engine.connect() as conn:
        row = conn.execute(text("SELECT state, author_association FROM run")).one()
    assert tuple(row) == ("done", None)

    store.record_blocked(_job("c" * 40), "c" * 40, Author("NONE", "outsider"), 556)
    assert store.last_comment_id("o/r", 1) == 556


def test_approval_is_recorded_once_per_comment(store: Store) -> None:
    store.add_approval("o/r", 1, "a" * 40, "maintainer", 9)
    store.add_approval("o/r", 1, "a" * 40, "maintainer", 9)  # 재시도
    store.add_approval("o/r", 2, "b" * 40, "maintainer", 10)
    assert store.approved_shas("o/r", 1) == {"a" * 40}
    assert store.approved_shas("o/r", 3) == set()


def test_rag_evidence_counts_this_repos_adopted_and_rejected_records(store: Store) -> None:
    for n, kind in enumerate(["adopted", "rejected", "adopted", "failed", "checker"]):
        store.add_record(record(kind, n))
    store.add_record(record("adopted", 9, repo="o/other"))
    store.add_record(NewRecord(
        repo="o/r", kind="adopted", origin="author_commit", source_id="seen:h:v",
        rule_id="R-1", rule_text="규칙", file="a.py", line_text="x", context="", diff="",
    ))  # fmt: skip
    assert store.rag_evidence("o/r") == Evidence(4)  # 수정안 PR 셋과 작성자 커밋 하나
    assert store.rag_evidence("o/other") == Evidence(1)
    assert store.rag_evidence("o/none") == Evidence(0)


def test_rag_evidence_reads_fix_pull_request_outcomes_of_this_repo(store: Store) -> None:
    fix_outcome(store, 1, "merged", "[5]")
    fix_outcome(store, 2, "rejected", "[5]")
    fix_outcome(store, 3, "merged", None)
    fix_outcome(store, 4, "open", None)
    fix_outcome(store, 5, "superseded", "[5]")
    fix_outcome(store, 6, "merged", None, bundle="mechanical")
    fix_outcome(store, 7, "merged", "[5]", repo="o/other")
    store.save_fix_attempt(FixAttempt(  # 수정안 PR이 없는 시도 (검증 실패)
        repo="o/r", pr_number=8, head_sha="h", merge_base="m", bundle="model", status="failed",
        attempts=3, models_json="[]", targets_json="[]", references_json="[5]",
    ))  # fmt: skip
    assert store.rag_evidence("o/r") == Evidence(0, Outcomes(1, 2), Outcomes(1, 1))
    assert store.rag_evidence("o/other") == Evidence(0, Outcomes(1, 1), Outcomes(0, 0))


def test_turned_off_is_one_row_per_repo_and_bound_to_the_comparison_threshold(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pipeline.db"
    store = Store(path, fakeredis.FakeStrictRedis())
    first = Evidence(25, Outcomes(9, 30), Outcomes(10, 30))
    assert store.rag_turned_off("o/r", 30, 10) is None
    store.turn_rag_off("o/r", first, 30, 10)
    store.turn_rag_off("o/r", first, 30, 10)  # 다시 불려도 한 줄
    # 워커를 다시 띄운 것과 같다: 새 연결이 같은 사실을 읽는다
    again = Store(path, fakeredis.FakeStrictRedis())
    assert again.rag_turned_off("o/r", 30, 10) == first
    assert again.rag_turned_off("o/r", 40, 10) is None  # 기준값을 바꾸면 다시 정한다
    assert again.rag_turned_off("o/r", 30, 5) is None
    assert again.rag_turned_off("o/other", 30, 10) is None
    with again.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM rag_auto_off")).scalar() == 1


def test_existing_database_gets_rag_auto_off_and_keeps_its_rows(tmp_path: Path) -> None:
    """이 작업 전의 데이터베이스(`rag_auto_off` 없음)를 그대로 연다. 있던 기록은 남고 auto가 그
    기록을 센다."""
    path = tmp_path / "pipeline.db"
    store = Store(path, fakeredis.FakeStrictRedis())
    for n in range(3):
        store.add_record(record("adopted", n))
    fix_outcome(store, 1, "merged", "[1]")
    old = create_engine(f"sqlite:///{path}")
    with old.begin() as conn:
        conn.execute(text("DROP TABLE rag_auto_off"))
    old.dispose()

    store = Store(path, fakeredis.FakeStrictRedis())
    assert "rag_auto_off" in inspect(store.engine).get_table_names()
    assert store.rag_evidence("o/r") == Evidence(3, Outcomes(1, 1), Outcomes(0, 0))
    assert store.rag_turned_off("o/r", 30, 10) is None
