from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import fakeredis
import pytest
from rq import Queue

from app import config
from app.controllers import sync
from app.models import state_keys
from app.models.job import PRJob
from app.services.github import GitHubReadError, OpenPR
from app.services.history import History
from app.worker.state import Store

HEAD: Final = "h" * 40


@pytest.fixture
def env(tmp_path: Path):
    redis = fakeredis.FakeStrictRedis()
    settings = config.PipelineConfig(repos=["o/r"], data_dir=tmp_path)
    return {
        "redis": redis,
        "settings": settings,
        "store": Store(settings.db_path, redis),
        "history": History(settings.db_path),
    }


def _pr(number: int = 1, head: str = HEAD, ref: str = "feature") -> OpenPR:
    return OpenPR(number=number, head_sha=head, base_sha="b" * 40, head_ref=ref, draft=False)


def _job(head: str = HEAD) -> PRJob:
    return PRJob(
        repo="o/r", pr_number=1, kind="update", head_sha=head, base_sha="b" * 40, source="sync"
    )


def _sync(env, prs, now=None):
    return sync.run_once(
        env["settings"], env["redis"], env["history"], list_prs=lambda repo: prs, now=now
    )


def _queued(env) -> list[PRJob]:
    return [
        PRJob.model_validate_json(j.args[0])
        for j in Queue(state_keys.QUEUE_NAME, connection=env["redis"]).jobs
    ]


def test_unseen_head_is_queued_as_sync_job(env) -> None:
    result = _sync(env, [_pr()])
    assert result.enqueued == [("o/r", 1, "no_record")]
    (job,) = _queued(env)
    assert (job.source, job.head_sha, job.delivery_id) == ("sync", HEAD, None)
    assert env["redis"].get(state_keys.latest_head("o/r", 1)) == HEAD.encode()


def test_done_head_is_not_queued_again(env) -> None:
    run = env["store"].start_run(_job())
    env["store"].finish_run(run, "done", test_status="passed")
    assert _sync(env, [_pr()]).enqueued == []


def test_env_error_waits_for_backoff(env) -> None:
    run = env["store"].start_run(_job())
    env["store"].finish_run(run, "env_error")
    assert _sync(env, [_pr()]).enqueued == []  # 방금 끝남
    later = datetime.now(UTC) + timedelta(minutes=31)
    assert _sync(env, [_pr()], now=later).enqueued == [("o/r", 1, "env_error_backoff")]


def test_dead_worker_job_key_is_cleared_and_requeued(env) -> None:
    env["store"].start_run(_job())  # running으로 남음
    env["redis"].set(state_keys.job(_job().dedup_key), "1")  # 죽은 워커가 남긴 키
    later = datetime.now(UTC) + timedelta(minutes=21)
    assert _sync(env, [_pr()], now=later).enqueued == [("o/r", 1, "dead_worker")]


def test_already_queued_job_is_not_duplicated(env) -> None:
    _sync(env, [_pr()])
    assert _sync(env, [_pr()]).enqueued == []  # 아직 처리 전이라 기록 없음, 하지만 키가 있다
    assert len(_queued(env)) == 1


def test_pipeline_branches_are_skipped(env) -> None:
    assert _sync(env, [_pr(ref="pipeline/pr-1-fix-lint")]).enqueued == []


def test_github_failure_skips_repo_without_raising(env) -> None:
    def broken(repo: str):
        raise GitHubReadError("gh 로그인 만료")

    result = sync.run_once(env["settings"], env["redis"], env["history"], list_prs=broken)
    assert result.failed_repos == ["o/r"] and result.enqueued == []
