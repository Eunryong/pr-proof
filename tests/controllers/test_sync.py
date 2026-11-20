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
from app.models.ops_alert import Alert
from app.notify import slack
from app.services.github import GitHubReadError, OpenPR, PRComment
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


@pytest.fixture
def alerts(monkeypatch) -> list[Alert]:
    """보낸 운영 알림. 보내기 자체는 tests/notify/test_slack.py가 본다."""
    sent: list[Alert] = []
    monkeypatch.setattr(slack, "send", lambda alert, redis, **kw: sent.append(alert) or "sent")
    return sent


def _pr(number: int = 1, head: str = HEAD, ref: str = "feature") -> OpenPR:
    return OpenPR(number=number, head_sha=head, base_sha="b" * 40, head_ref=ref, draft=False)


def _job(head: str = HEAD) -> PRJob:
    return PRJob(
        repo="o/r", pr_number=1, kind="update", head_sha=head, base_sha="b" * 40, source="sync"
    )


HEAD_TIME: Final = "2026-11-17T10:00:00Z"


def _comment(cid: int, body: str, assoc: str = "MEMBER", at: str = "2026-11-17T10:05:00Z"):
    return PRComment(id=cid, author_association=assoc, created_at=at, body=body)


def _sync(env, prs, now=None, comments=()):
    return sync.run_once(
        env["settings"], env["redis"], env["history"], list_prs=lambda repo: prs,
        head_time=lambda repo, sha: HEAD_TIME,
        list_comments=lambda repo, n, since: [c for c in comments if c.created_at >= since],
        now=now,
    )  # fmt: skip


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


def test_author_values_are_carried_for_the_record(env) -> None:
    """동기화도 판단하지 않고 실어 넘기기만 한다 (8.6). `gh pr list`에는 PR 작성자의
    author_association이 없어 로그인 이름만 싣는다."""
    pr = OpenPR(number=1, head_sha=HEAD, base_sha="b" * 40, head_ref="f", draft=False,
                author_login="outsider")  # fmt: skip
    comment = PRComment(id=7, author_association="MEMBER", created_at="2026-11-17T10:05:00Z",
                        body="/fix", login="maintainer")  # fmt: skip
    _sync(env, [pr], comments=[comment])
    command, update = sorted(_queued(env), key=lambda j: j.kind)
    assert (update.author_association, update.author_login) == (None, "outsider")
    assert (command.author_login, command.commenter_association, command.commenter_login) == (
        "outsider", "MEMBER", "maintainer",
    )  # fmt: skip


def test_blocked_head_is_not_queued_again(env) -> None:
    """승인을 기다리는 외부 작성자의 PR은 주기마다 다시 넣지 않는다 (8.6)."""
    run = env["store"].start_run(_job())
    env["store"].finish_run(run, "blocked")
    assert _sync(env, [_pr()]).enqueued == []


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


def test_dead_worker_job_key_is_cleared_and_requeued(env, alerts) -> None:
    env["store"].start_run(_job())  # running으로 남음
    env["redis"].set(state_keys.job(_job().dedup_key), "1")  # 죽은 워커가 남긴 키
    later = datetime.now(UTC) + timedelta(minutes=21)
    assert _sync(env, [_pr()], now=later).enqueued == [("o/r", 1, "dead_worker")]
    assert alerts == [Alert("worker_lost", "o/r", 1)]


def test_already_queued_job_is_not_duplicated(env) -> None:
    _sync(env, [_pr()])
    assert _sync(env, [_pr()]).enqueued == []  # 아직 처리 전이라 기록 없음, 하지만 키가 있다
    assert len(_queued(env)) == 1


def test_pipeline_branches_are_skipped(env) -> None:
    assert _sync(env, [_pr(ref="pipeline/pr-1-fix-lint")]).enqueued == []


def test_github_failure_skips_repo_without_raising(env, alerts) -> None:
    def broken(repo: str):
        raise GitHubReadError("gh 로그인 만료")

    result = sync.run_once(env["settings"], env["redis"], env["history"], list_prs=broken)
    assert result.failed_repos == ["o/r"] and result.enqueued == []
    assert alerts == [Alert("sync_failed", "o/r")]


# --- 놓친 명령 ---------------------------------------------------------------------


def _commands(env) -> list[PRJob]:
    return [j for j in _queued(env) if j.kind == "command"]


def test_missed_command_is_queued(env) -> None:
    result = _sync(env, [_pr()], comments=[_comment(5, "/recheck\n설명")])
    (job,) = _commands(env)
    assert (job.command, job.comment_id, job.source) == ("/recheck", 5, "sync")
    assert ("o/r", 1, "missed_command") in result.enqueued


@pytest.mark.parametrize(
    "comment",
    [
        _comment(6, "/fix lint", assoc="CONTRIBUTOR"),
        _comment(7, "/fix lint", assoc="NONE"),
        _comment(12, "/fix model", assoc="CONTRIBUTOR"),
        _comment(8, "<!-- pr-proof -->\n/recheck"),
        _comment(9, "<!-- pr-proof:reply -->\n> /fix model"),
        _comment(10, "LGTM"),
        _comment(11, "/fix lint", at="2026-11-17T09:59:59Z"),  # 지금 head보다 먼저 단 명령
    ],
    ids=[
        "contributor",
        "none",
        "fix-model-contributor",
        "own-result",
        "own-reply",
        "not-command",
        "before-head",
    ],
)
def test_missed_command_filters_like_webhook(env, comment) -> None:
    _sync(env, [_pr()], comments=[comment])
    assert _commands(env) == []


def test_handled_command_is_not_queued_again(env) -> None:
    command = PRJob(
        repo="o/r", pr_number=1, kind="command", command="/recheck", comment_id=5, source="webhook"
    )
    env["store"].record_command(command, "recheck:passed")
    _sync(env, [_pr()], comments=[_comment(5, "/recheck")])
    assert _commands(env) == []


def test_comment_lookup_failure_still_syncs_heads(env) -> None:
    def broken(repo, n, since):
        raise GitHubReadError("502")

    sync.run_once(
        env["settings"], env["redis"], env["history"], list_prs=lambda repo: [_pr()],
        head_time=lambda repo, sha: HEAD_TIME, list_comments=broken,
    )  # fmt: skip
    assert [j.kind for j in _queued(env)] == ["update"]


def test_webhook_and_sync_share_the_filter() -> None:
    """권한 확인이 한쪽에만 있으면 그쪽이 구멍이 된다. 같은 함수를 쓰는지 본다."""
    from app.controllers import webhook
    from app.models import comment

    assert webhook.command_in_comment is comment.command_in_comment
    assert sync.command_in_comment is comment.command_in_comment


def test_closed_pr_with_open_proposal_gets_close_job(env) -> None:
    """close 웹훅을 놓쳐도 열린 제안이 남은 닫힌 PR은 close 작업으로 정리한다."""
    env["store"].record_proposal("o/r", 7, "lint", HEAD, "pipeline/pr-7-fix-lint", 900)
    env["store"].record_proposal("o/r", 1, "lint", HEAD, "pipeline/pr-1-fix-lint", 901)
    result = _sync(env, [_pr(1)])  # 7번은 이제 열린 PR 목록에 없다
    closes = [j for j in _queued(env) if j.kind == "close"]
    assert [(j.pr_number, j.source) for j in closes] == [(7, "sync")]
    assert ("o/r", 7, "closed_pr") in result.enqueued
