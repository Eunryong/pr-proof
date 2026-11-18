from pathlib import Path

import fakeredis
import pytest

from app.models import state_keys
from app.models.job import PRJob
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
