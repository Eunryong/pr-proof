from pathlib import Path

import fakeredis

from app.models.job import PRJob
from app.services.history import History
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
