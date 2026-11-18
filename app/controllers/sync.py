"""동기화 한 번: 열린 PR의 head ↔ 처리 기록 비교 → 빠진 것만 PRJob → 큐 (docs/DESIGN.md F2).

웹훅을 놓쳤을 때(로컬 서버가 꺼져 있던 동안 등) 보충한다. 다시 넣을지는 models.sync_rules가 정한다.
닫힌 PR 처리와 놓친 명령 보충은 워커가 그 작업을 처리하게 될 때 붙인다 (docs/BACKLOG.md).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from redis import Redis

from app.config import PipelineConfig
from app.controllers.webhook import PIPELINE_BRANCH_PREFIX
from app.models.job import PRJob
from app.models.sync_rules import EnqueueReason, enqueue_reason
from app.services import github, queue
from app.services.history import History

log = logging.getLogger(__name__)


@dataclass
class SyncResult:
    enqueued: list[tuple[str, int, EnqueueReason]] = field(default_factory=list)
    failed_repos: list[str] = field(default_factory=list)


def run_once(
    settings: PipelineConfig,
    redis: Redis,
    history: History,
    *,
    list_prs: Callable[[str], list[github.OpenPR]] = github.list_open_prs,
    now: datetime | None = None,
) -> SyncResult:
    now = now or datetime.now(UTC)
    backoff = timedelta(minutes=settings.sync.env_error_backoff_minutes)
    heartbeat_timeout = timedelta(minutes=settings.sync.heartbeat_timeout_minutes)
    result = SyncResult()
    for repo in settings.repos:
        try:
            prs = list_prs(repo)
        except github.GitHubReadError as e:
            log.warning("동기화: %s 조회 실패, 다음 주기에 다시 한다: %s", repo, e)
            result.failed_repos.append(repo)
            continue
        for pr in prs:
            if pr.head_ref.startswith(PIPELINE_BRANCH_PREFIX):
                continue
            runs = history.runs_for(repo, pr.number, pr.head_sha)
            reason = enqueue_reason(
                runs, now, env_error_backoff=backoff, heartbeat_timeout=heartbeat_timeout
            )
            if reason is None:
                continue
            job = PRJob(
                repo=repo,
                pr_number=pr.number,
                kind="update",
                head_sha=pr.head_sha,
                base_sha=pr.base_sha,
                source="sync",
            )
            if reason == "dead_worker":
                queue.clear_job(redis, job)
            if queue.enqueue(redis, job) == "queued":
                result.enqueued.append((repo, pr.number, reason))
    return result
