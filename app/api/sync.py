"""동기화 한 번: 열린 PR의 head ↔ 처리 기록 비교 → 빠진 것만 PRJob → 큐 (docs/DESIGN.md F2).

웹훅을 놓쳤을 때(로컬 서버가 꺼져 있던 동안 등) 보충한다. 다시 넣을지는 sync_rules_pure가 정한다.
놓친 명령은 현재 head 커밋 뒤에 달린 것 중 워커가 처리하지 않은 것만 넣는다. 권한 확인은
웹훅과 같은 함수(app/shared/comment_pure.command_in_comment)로 한다.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from redis import Redis

from app.api import github, queue
from app.api.history import History
from app.api.sync_rules_pure import EnqueueReason, enqueue_reason
from app.api.webhook import PIPELINE_BRANCH_PREFIX
from app.shared import github_auth, version
from app.shared import notify as macos
from app.shared.comment_pure import command_in_comment
from app.shared.config import PipelineConfig
from app.shared.feedback_pure import feedback_in_comment
from app.shared.job_pure import PRJob
from app.shared.ops_alert_pure import Kind

log = logging.getLogger(__name__)


@dataclass
class SyncResult:
    enqueued: list[tuple[str, int, EnqueueReason | str]] = field(default_factory=list)
    failed_repos: list[str] = field(default_factory=list)
    # 사람이 해야 풀리는 환경 오류로 보이는 것. 조회가 실패했고 `gh auth status`도 실패하면 gh_login
    # (토큰 방식이면 gh_token)
    environment: frozenset[Kind] = frozenset()


def run_once(
    settings: PipelineConfig,
    redis: Redis,
    history: History,
    *,
    list_prs: Callable[[str], list[github.OpenPR]] = github.list_open_prs,
    head_time: Callable[[str, str], str] = github.head_commit_time,
    list_comments: Callable[[str, int, str], list[github.PRComment]] = github.list_comments_since,
    list_feedback: Callable[[str, int], list[github.FeedbackItem]] = github.list_feedback,
    auth_ok: Callable[[], bool] = github.auth_ok,
    auth_kind: Callable[[], Kind] = github_auth.alert_kind,
    now: datetime | None = None,
    pipeline_commit: str | None = None,
) -> SyncResult:
    now = now or datetime.now(UTC)
    if pipeline_commit is None:
        pipeline_commit = version.current()
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
        # 받아들인 코멘트의 수정안이 머지됐는데 웹훅을 놓쳤다. 원래 PR이 이미 닫혔어도 넣는다
        # (수정안을 머지한 뒤 원래 PR도 머지됐을 수 있다). 머지됐는지는 워커가 GitHub에서 본다
        for number in sorted(history.prs_awaiting_resolve(repo)):
            job = PRJob(repo=repo, pr_number=number, kind="resolve", source="sync")
            if queue.enqueue(redis, job) == "queued":
                result.enqueued.append((repo, number, "awaiting_resolve"))
        for pr in prs:
            if pr.head_ref.startswith(PIPELINE_BRANCH_PREFIX):
                continue
            try:
                _missed_commands(repo, pr, redis, history, result, head_time, list_comments)
            except github.GitHubReadError as e:
                log.warning("동기화: %s#%s 명령 조회 실패: %s", repo, pr.number, e)
            runs = history.runs_for(repo, pr.number, pr.head_sha)
            # 승인을 기다리는 외부 작성자의 PR은 코멘트도 처리하지 않는다. 넣으면 주기마다
            # 승인 안내를 다시 달고 blocked 기록이 쌓인다 (8.6)
            if not (runs and runs[-1].state == "blocked"):
                try:
                    _unhandled_feedback(repo, pr, redis, history, result, list_feedback)
                except github.GitHubReadError as e:
                    log.warning("동기화: %s#%s 코멘트 조회 실패: %s", repo, pr.number, e)
            reason = enqueue_reason(
                runs, now, env_error_backoff=backoff, heartbeat_timeout=heartbeat_timeout,
                pipeline_commit=pipeline_commit,
            )  # fmt: skip
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
    if result.failed_repos and not auth_ok():
        result.environment = frozenset({auth_kind()})
    return result


def alert_lasting(previous: frozenset[Kind], current: frozenset[Kind], data_dir: Path) -> None:
    """동기화가 두 주기 연속으로 본 환경 오류만 알린다. 한 번의 네트워크 끊김은 다음 주기에 풀리고,
    워커의 재시도에 해당하는 것이 동기화에는 다음 주기뿐이다. 같은 종류는 30분에 한 번이다."""
    for kind in sorted(previous & current):
        macos.send(kind, data_dir)


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


def _unhandled_feedback(
    repo: str,
    pr: github.OpenPR,
    redis: Redis,
    history: History,
    result: SyncResult,
    list_feedback: Callable[[str, int], list[github.FeedbackItem]],
) -> None:
    """다룰 코멘트 중 워커가 처리하지 않은 것이 하나라도 있으면 PR마다 feedback 작업 하나를 넣는다.

    어떤 코멘트인지는 싣지 않는다(워커가 GitHub에서 읽는다). 웹훅이 오지 않았거나 이 기능이 생기기
    전에 달린 코멘트도 여기서 걸리므로 `/recheck`가 필요 없다. 거르는 규칙은 웹훅과 같은 함수다.
    """
    handled = history.handled_feedback_keys(repo, pr.number, pr.head_sha)
    for item in list_feedback(repo, pr.number):
        if (item.kind, item.id) in handled:
            continue
        if not feedback_in_comment(item.body, item.author_association, item.login)[0]:
            continue
        job = PRJob(
            repo=repo,
            pr_number=pr.number,
            kind="feedback",
            head_sha=pr.head_sha,
            base_sha=pr.base_sha,
            source="sync",
            author_login=pr.author_login or None,
        )
        if queue.enqueue(redis, job) == "queued":
            result.enqueued.append((repo, pr.number, "unhandled_feedback"))
        return
