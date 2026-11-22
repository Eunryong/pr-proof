import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import fakeredis

from app.api.history import History
from app.shared.job_pure import PRJob
from app.worker.state import Store


def test_reads_runs_written_by_worker_in_order(tmp_path: Path) -> None:
    db = tmp_path / "pipeline.db"
    store = Store(db, fakeredis.FakeStrictRedis())
    job = PRJob(
        repo="o/r", pr_number=1, kind="update", head_sha="h", base_sha="b", source="webhook"
    )
    first = store.start_run(job)
    store.finish_run(first, "env_error")
    store.start_run(job)
    runs = History(db).runs_for("o/r", 1, "h")
    assert [r.state for r in runs] == ["env_error", "running"]
    assert History(db).runs_for("o/r", 1, "other") == []


def test_missing_db_means_no_history(tmp_path: Path) -> None:
    assert History(tmp_path / "none.db").runs_for("o/r", 1, "h") == []


def _record(store: Store, item_id: int, **fields) -> None:
    base = {"repo": "o/r", "pr_number": 1, "kind": "review_comment", "item_id": item_id,
            "thread_id": f"T{item_id}", "login": "Copilot", "head_sha": "h",
            "verdict": "accepted", "proposal_number": 900}  # fmt: skip
    store.record_feedback(**(base | fields))


def test_handled_feedback_keys_are_kind_and_id_of_the_pr(tmp_path: Path) -> None:
    db = tmp_path / "pipeline.db"
    store = Store(db, fakeredis.FakeStrictRedis())
    _record(store, 1)
    _record(store, 2, kind="review", thread_id=None, verdict="no_action", proposal_number=None)
    _record(store, 3, pr_number=2)
    history = History(db)
    assert history.handled_feedback_keys("o/r", 1) == {("review_comment", 1), ("review", 2)}
    assert history.handled_feedback_keys("o/r", 2) == {("review_comment", 3)}
    assert history.handled_feedback_keys("x/y", 1) == set()


def test_prs_awaiting_resolve(tmp_path: Path) -> None:
    db = tmp_path / "pipeline.db"
    store = Store(db, fakeredis.FakeStrictRedis())
    _record(store, 1)  # 기다리는 중
    _record(store, 2, pr_number=2, verdict="rejected", proposal_number=None)
    _record(store, 3, pr_number=3, thread_id=None)  # 스레드가 없다
    _record(store, 4, pr_number=4, proposal_number=None)  # 수정안이 없다
    _record(store, 5, pr_number=5)
    (done,) = [f for f in store.handled_feedback("o/r", 5)]
    store.mark_feedback_resolved([done.id], datetime.now(UTC))  # 이미 해결함
    _record(store, 6, repo="x/y", pr_number=6)
    assert History(db).prs_awaiting_resolve("o/r") == {1}


def test_closed_unmerged_proposal_is_not_awaited_but_open_and_merged_are(tmp_path: Path) -> None:
    """머지되지 않고 닫힌 수정안은 스레드를 바꾸지 않으므로 동기화가 계속 넣을 이유가 없다."""
    db = tmp_path / "pipeline.db"
    store = Store(db, fakeredis.FakeStrictRedis())
    states = {1: "open", 2: "merged", 3: "rejected", 4: "superseded", 5: "closed_with_pr"}
    for pr, state in states.items():
        _record(store, pr, pr_number=pr, proposal_number=900 + pr)
        store.record_proposal("o/r", pr, "model", "h", f"pipeline/pr-{pr}-fix-model", 900 + pr)
        (p,) = store.proposals("o/r", pr)
        if state != "open":
            store.set_proposal_state(p.id, state)
    assert History(db).prs_awaiting_resolve("o/r") == {1, 2}


def test_missing_db_or_table_means_no_feedback_history(tmp_path: Path) -> None:
    assert History(tmp_path / "none.db").handled_feedback_keys("o/r", 1) == set()
    assert History(tmp_path / "none.db").prs_awaiting_resolve("o/r") == set()
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()  # 파일은 있고 테이블이 없다 (워커가 아직 스키마를 만들지 않음)
    assert History(empty).handled_feedback_keys("o/r", 1) == set()
    assert History(empty).prs_awaiting_resolve("o/r") == set()
