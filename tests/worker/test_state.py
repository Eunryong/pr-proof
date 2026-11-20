from pathlib import Path

import fakeredis
import pytest
from sqlalchemy import create_engine, inspect, text

from app.models import state_keys
from app.models.db import AICall, SeenViolation
from app.models.job import PRJob
from app.models.records import NewRecord
from app.worker.state import Store


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


def test_rejected_targets_come_only_from_closed_proposals(store: Store) -> None:
    key = ("a.py", "F401", "import sys")
    store.record_proposal("o/r", 1, "lint", "h1", "pipeline/pr-1-fix-lint", 900, [key, key])
    store.record_proposal("o/r", 1, "static", "h1", "pipeline/pr-1-fix-static", 901,
                          [("b.py", "PLR2004", "if n > 3:")])  # fmt: skip
    assert store.rejected_targets("o/r", 1) == {}  # 열려 있는 제안은 거절이 아니다
    lint_row, static_row = store.proposals("o/r", 1)
    store.set_proposal_state(lint_row.id, "rejected")
    store.set_proposal_state(static_row.id, "superseded")  # 파이프라인이 닫은 것은 거절이 아니다
    assert store.rejected_targets("o/r", 1) == {key: 900}
    assert store.rejected_targets("o/r", 2) == {}


def test_reproposal_replaces_targets(store: Store) -> None:
    store.record_proposal("o/r", 1, "lint", "h1", "b", 900, [("a.py", "F401", "import os")])
    store.record_proposal("o/r", 1, "lint", "h2", "b", 902, [("a.py", "F401", "import sys")])
    (row,) = store.proposals("o/r", 1)
    store.set_proposal_state(row.id, "rejected")
    assert store.rejected_targets("o/r", 1) == {("a.py", "F401", "import sys"): 902}


def test_new_nullable_columns_are_added_to_an_existing_table(tmp_path: Path) -> None:
    path = tmp_path / "pipeline.db"
    old = create_engine(f"sqlite:///{path}")
    with old.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE aicall (id INTEGER PRIMARY KEY, repo VARCHAR NOT NULL, "
                "pr_number INTEGER NOT NULL, head_sha VARCHAR NOT NULL, skill VARCHAR NOT NULL, "
                "model VARCHAR NOT NULL, attempt INTEGER NOT NULL, ok BOOLEAN NOT NULL, "
                "error VARCHAR, cost_usd FLOAT, charged_usd FLOAT NOT NULL, "
                "permission_denials INTEGER NOT NULL, created_at DATETIME)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO aicall (repo, pr_number, head_sha, skill, model, attempt, ok, "
                "charged_usd, permission_denials) VALUES ('o/r', 1, 'a', 'static', 'haiku', 1, 1, "
                "0.1, 0)"
            )
        )
    old.dispose()

    store = Store(path, fakeredis.FakeStrictRedis())
    Store(path, fakeredis.FakeStrictRedis())  # 다시 열어도 같은 상태
    columns = {c["name"] for c in inspect(store.engine).get_columns("aicall")}
    assert {"unexpected_tools", "detail"} <= columns
    store.record_ai_call(
        AICall(
            repo="o/r",
            pr_number=1,
            head_sha="b",
            skill="static",
            model="haiku",
            attempt=2,
            ok=False,
            error="tools",
            charged_usd=0.3,
            unexpected_tools='{"Bash": 2}',
            detail="도구 목록이 기대와 다름",
        )
    )
    assert store.ai_spent("o/r", 1) == pytest.approx(0.4)  # 옛 행도 그대로 읽힌다


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
