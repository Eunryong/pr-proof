"""동기화 한 번: 열린 PR의 head ↔ 처리 기록 비교 → 빠진 것만 PRJob → 큐 (docs/DESIGN.md F2).

웹훅을 놓쳤을 때(로컬 서버가 꺼져 있던 동안 등) 보충한다. 다시 넣을지는 models.sync_rules가 정한다.
놓친 명령은 현재 head 커밋 뒤에 달린 것 중 워커가 처리하지 않은 것만 넣는다. 권한 확인은
웹훅과 같은 함수(models.comment.command_in_comment)로 한다.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from redis import Redis

from app.config import PipelineConfig
from app.controllers.webhook import PIPELINE_BRANCH_PREFIX
from app.models.comment import command_in_comment
from app.models.job import PRJob
from app.models.sync_rules import EnqueueReason, enqueue_reason
from app.services import github, queue
from app.services.history import History

log = logging.getLogger(__name__)


@dataclass
class SyncResult:
    enqueued: list[tuple[str, int, EnqueueReason | str]] = field(default_factory=list)
    failed_repos: list[str] = field(default_factory=list)


def run_once(
    settings: PipelineConfig,
    redis: Redis,
    history: History,
    *,
    list_prs: Callable[[str], list[github.OpenPR]] = github.list_open_prs,
    head_time: Callable[[str, str], str] = github.head_commit_time,
    list_comments: Callable[[str, int, str], list[github.PRComment]] = github.list_comments_since,
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
        open_numbers = {pr.number for pr in prs}
        for number in sorted(history.prs_with_open_proposals(repo) - open_numbers):
            # 원본 PR이 닫혔는데 close 웹훅을 놓쳤다. 워커가 남은 제안 PR을 닫는다
            job = PRJob(repo=repo, pr_number=number, kind="close", source="sync")
            if queue.enqueue(redis, job) == "queued":
                result.enqueued.append((repo, number, "closed_pr"))
        for pr in prs:
            if pr.head_ref.startswith(PIPELINE_BRANCH_PREFIX):
                continue
            try:
                _missed_commands(repo, pr, redis, history, result, head_time, list_comments)
            except github.GitHubReadError as e:
                log.warning("동기화: %s#%s 명령 조회 실패: %s", repo, pr.number, e)
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
                author_login=pr.author_login or None,
            )
            if reason == "dead_worker":
                queue.clear_job(redis, job)
            if queue.enqueue(redis, job) == "queued":
                result.enqueued.append((repo, pr.number, reason))
    return result


def _missed_commands(
    repo: str,
    pr: github.OpenPR,
    redis: Redis,
    history: History,
    result: SyncResult,
    head_time: Callable[[str, str], str],
    list_comments: Callable[[str, int, str], list[github.PRComment]],
) -> None:
    """현재 head 뒤에 달린, 아직 처리하지 않은 명령을 넣는다.

    이전 커밋에 단 /fix를 뒤늦게 실행하지 않는다.
    """
    since = head_time(repo, pr.head_sha)
    handled = history.handled_command_ids(repo, pr.number)
    for comment in list_comments(repo, pr.number, since):
        command, _ = command_in_comment(comment.body, comment.author_association)
        if command is None or comment.id in handled:
            continue
        job = PRJob(
            repo=repo,
            pr_number=pr.number,
            kind="command",
            command=command,
            comment_id=comment.id,
            source="sync",
            author_login=pr.author_login or None,
            commenter_association=comment.author_association or None,
            commenter_login=comment.login or None,
        )
        if queue.enqueue(redis, job) == "queued":
            result.enqueued.append((repo, pr.number, "missed_command"))
