"""외부 작성자 PR 가드레일 (DESIGN 8.6, 위협 표 T2·T18, 완료 기준 6-8–6-11).

신뢰되지 않은 작성자의 PR은 어느 경로로 들어와도 워커의 같은 확인을 지나고, 체크아웃·샌드박스·
모델 없이 승인 안내 한 줄만 남긴다. 판단은 워커가 GitHub에서 읽은 값으로 한다. 승인은 신뢰된
사람이 지금 head를 적어 단 `/recheck <커밋>`뿐이다.
"""

import functools
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import pytest
from rq import Queue
from sqlmodel import Session, select

from api import signature, sync, webhook
from api.github import OpenPR
from api.history import History
from shared import state_keys_pure as state_keys
from shared.comment_pure import COMMENT_MARKER
from shared.db_pure import Approval, Run
from shared.job_pure import PRJob
from worker import base_config, cli, graph, guard, main, repo, retry
from worker.access_pure import AccessSettings
from worker.ai import claude_cli
from worker.github import GitHubError
from worker.repo import GitError
from worker.sandbox import docker_cli
from worker.sandbox.docker_cli import SandboxError

from .helpers import HEAD, PASSED, use_runner
from .test_github import is_reply

NEW_HEAD: Final = "c" * 40
BASE_SHA: Final = "b" * 40
SECRET: Final = "s3cret"
COMMENT_ID: Final = 9001
UNTRUSTED: Final = [
    "CONTRIBUTOR",
    "FIRST_TIME_CONTRIBUTOR",
    "FIRST_TIMER",
    "NONE",
    "SOMETHING_NEW",
    "",
]


def _update(head: str = HEAD, **extra) -> str:
    return PRJob(
        repo="o/r", pr_number=1, kind="update", head_sha=head, base_sha=BASE_SHA,
        source="webhook", **extra,
    ).model_dump_json()  # fmt: skip


def _command(text: str, comment_id: int = COMMENT_ID) -> str:
    return PRJob(
        repo="o/r", pr_number=1, kind="command", command=text, comment_id=comment_id,
        source="sync",
    ).model_dump_json()  # fmt: skip


def _forbid_everything(env) -> None:
    """체크아웃, 샌드박스(docker), 모델 호출, 그래프 실행·재개가 불리면 실패한다."""
    patch = env["monkeypatch"].setattr
    patch(repo, "checkout", lambda *a, **k: pytest.fail("체크아웃하면 안 된다"))
    patch(graph, "run_flow", lambda *a, **k: pytest.fail("검사를 돌리면 안 된다"))
    patch(graph, "resume_fix", lambda *a, **k: pytest.fail("수정을 재개하면 안 된다"))
    patch(graph, "run_pr_tests", lambda *a, **k: pytest.fail("테스트를 돌리면 안 된다"))
    patch(docker_cli, "run", lambda *a, **k: pytest.fail("컨테이너를 쓰면 안 된다"))
    patch(docker_cli, "minimal_env", lambda *a, **k: pytest.fail("컨테이너를 쓰면 안 된다"))
    patch(claude_cli, "run_claude", lambda *a, **k: pytest.fail("모델을 부르면 안 된다"))
    patch(claude_cli, "run_structured", lambda *a, **k: pytest.fail("모델을 부르면 안 된다"))


def _writes(gh, since: int = 0) -> list[tuple[list[str], str | None]]:
    """조회가 아닌 gh 호출."""
    return [(a, s) for a, s in gh.calls[since:] if s is not None or "-X" in a]


def _assert_only_the_guide(gh, head: str, since: int = 0) -> None:
    """GitHub에 남긴 것이 결과 코멘트 하나의 승인 안내 한 줄뿐이다."""
    writes = _writes(gh, since)
    assert len(writes) == 1, writes
    args, stdin = writes[0]
    assert "issues/1/comments" in " ".join(args) or "issues/comments/" in " ".join(args)
    body = json.loads(stdin)["body"]
    assert body.split("\n") == [COMMENT_MARKER, guard.approval_guide(head)]
    assert f"`/recheck {head}`" in body  # 승인에 그대로 복사할 수 있는 전체 해시
    # PR의 제목·본문(작성자의 글)은 가드레일이 들인 작업의 PR 단위 확인만 읽는다 (4.5 끝, T27)
    assert gh.text_lookups == 0
    assert not any(".title" in " ".join(args) for args, _ in gh.calls)


def _runs(env) -> list[Run]:
    with Session(env["deps"].store.engine) as session:
        return list(session.exec(select(Run).order_by(Run.id)).all())


def _approvals(env) -> list[Approval]:
    with Session(env["deps"].store.engine) as session:
        return list(session.exec(select(Approval)).all())


def _queued_payloads(env) -> list[str]:
    return [j.args[0] for j in Queue(state_keys.QUEUE_NAME, connection=env["redis"]).jobs]


def _history(env) -> History:
    return History(Path(env["deps"].store.engine.url.database))


def _access(env, access: AccessSettings) -> None:
    env["monkeypatch"].setattr(base_config, "load_access", lambda *a, **k: access)


def _approver(env, login: str = "maintainer", association: str = "MEMBER",
              body: str | None = None, pr: int = 1) -> None:  # fmt: skip
    """승인 코멘트를 GitHub에서 읽은 값. 작업에 실린 값과 관계없다."""
    env["gh"].issue_comments[COMMENT_ID] = {
        "login": login, "association": association,
        "body": body if body is not None else f"/recheck {env['gh'].head}", "pr": pr,
    }  # fmt: skip


# --- 6-9: 모든 경로가 같은 확인을 지난다 ---


def _via_webhook_pr(env) -> None:
    payload = {
        "action": "opened",
        "pull_request": {
            "number": 1, "head": {"ref": "feature", "sha": NEW_HEAD},
            "base": {"ref": "main", "sha": BASE_SHA},
            # 작업에 실리는 값은 OWNER지만 판단은 워커가 읽은 값으로 한다 (6-10)
            "author_association": "OWNER", "user": {"login": "outsider"},
        },
        "repository": {"full_name": "o/r"},
    }  # fmt: skip
    _deliver(env, "pull_request", payload)


def _via_webhook_fix(env) -> None:
    payload = {
        "action": "created",
        "issue": {"number": 1, "pull_request": {}, "author_association": "OWNER",
                  "user": {"login": "outsider"}},
        "comment": {"id": COMMENT_ID, "body": "/fix", "author_association": "OWNER",
                    "user": {"login": "maintainer"}},
        "repository": {"full_name": "o/r"},
    }  # fmt: skip
    _deliver(env, "issue_comment", payload)


def _deliver(env, event: str, payload: dict) -> None:
    body = json.dumps(payload).encode()
    response = webhook.handle(
        body=body, payload=payload, event=event, delivery_id=f"d-{event}",
        signature_header=signature.sign(SECRET, body), secret=SECRET,
        allowed_repos=frozenset({"o/r"}), redis=env["redis"],
    )  # fmt: skip
    assert response.body["status"] == "queued"


def _open_pr() -> OpenPR:
    return OpenPR(number=1, head_sha=NEW_HEAD, base_sha=BASE_SHA, head_ref="feature",
                  draft=False, author_login="outsider")  # fmt: skip


def _sync_once(env, now: datetime | None = None) -> sync.SyncResult:
    return sync.run_once(
        env["deps"].settings, env["redis"], _history(env), list_prs=lambda r: [_open_pr()],
        head_time=lambda r, sha: "2026-11-20T00:00:00Z", list_comments=lambda r, n, s: [],
        now=now,
    )  # fmt: skip


def _via_sync_loop(env) -> None:
    assert _sync_once(env).enqueued == [("o/r", 1, "no_record")]


def _via_cli_sync(env) -> None:
    run_once = functools.partial(
        sync.run_once, list_prs=lambda r: [_open_pr()],
        head_time=lambda r, sha: "2026-11-20T00:00:00Z", list_comments=lambda r, n, s: [],
    )  # fmt: skip
    patch = env["monkeypatch"].setattr
    patch(cli.config, "load_config", lambda: env["deps"].settings)
    patch(cli.Redis, "from_url", lambda url: env["redis"])
    patch(cli, "History", lambda path: _history(env))
    patch(cli.sync, "run_once", run_once)
    assert cli.main(["sync"]) == 0


def _requeue(state: str) -> Callable[[object], None]:
    """동기화가 같은 head를 다시 넣는다 (환경 오류 뒤, 죽은 워커). 앞의 시도는 신뢰될 때
    시작했다."""

    def via(env) -> None:
        store = env["deps"].store
        job = PRJob.model_validate_json(_update(NEW_HEAD))
        run_id = store.start_run(job)
        if state == "env_error":
            store.finish_run(run_id, "env_error")
        later = datetime.now(UTC) + timedelta(days=1)
        assert _sync_once(env, now=later).enqueued[0][2] in ("env_error_backoff", "dead_worker")

    return via


PATHS: Final = {
    "webhook": _via_webhook_pr,
    "sync_loop": _via_sync_loop,
    "cli_sync": _via_cli_sync,
    "requeue_env_error": _requeue("env_error"),
    "requeue_dead_worker": _requeue("running"),
    "fix": _via_webhook_fix,
}


@pytest.mark.parametrize("path", PATHS)
def test_untrusted_pr_from_every_path_leaves_only_the_guide(env, path: str) -> None:
    env["gh"].head, env["gh"].author_association = NEW_HEAD, "NONE"
    PATHS[path](env)
    payloads = _queued_payloads(env)
    assert len(payloads) == 1
    _forbid_everything(env)
    before = len(_runs(env))

    assert main.handle(payloads[0], env["deps"]) == guard.NEEDS_APPROVAL

    _assert_only_the_guide(env["gh"], NEW_HEAD)
    blocked = _runs(env)[before:]
    assert [(r.state, r.head_sha, r.author_association) for r in blocked] == [
        ("blocked", NEW_HEAD, "NONE")
    ]
    assert not _approvals(env)


def test_resumed_job_after_trust_was_lost_is_blocked(env) -> None:
    """RQ가 다시 시도하는 작업(멈춘 thread를 잇는 재개)도 시작에서 같은 확인을 지난다."""

    def broken(*a, **k):
        raise SandboxError("docker 꺼짐")

    use_runner(env, broken)
    env["monkeypatch"].setattr(retry, "attempt", lambda: (1, False))
    with pytest.raises(SandboxError):
        main.handle(_update(), env["deps"])  # 신뢰될 때 시작해 환경 오류로 멈췄다
    env["gh"].author_association = "NONE"
    env["monkeypatch"].setattr(retry, "attempt", lambda: (2, False))
    _forbid_everything(env)
    since = len(env["gh"].calls)

    assert main.handle(_update(), env["deps"]) == guard.NEEDS_APPROVAL

    _assert_only_the_guide(env["gh"], HEAD, since)  # 결과 코멘트를 안내 한 줄로 고쳐 쓴다
    assert [r.state for r in _runs(env)] == ["env_error", "blocked"]


@pytest.mark.parametrize("association", UNTRUSTED)
def test_every_untrusted_association_is_blocked(env, association: str) -> None:
    env["gh"].author_association = association
    _forbid_everything(env)
    assert main.handle(_update(), env["deps"]) == guard.NEEDS_APPROVAL
    _assert_only_the_guide(env["gh"], HEAD)


def test_new_commit_rewrites_the_same_guide_with_the_new_hash(env) -> None:
    env["gh"].author_association = "NONE"
    _forbid_everything(env)
    main.handle(_update(), env["deps"])
    env["gh"].head = NEW_HEAD
    since = len(env["gh"].calls)
    assert main.handle(_update(NEW_HEAD), env["deps"]) == guard.NEEDS_APPROVAL
    _assert_only_the_guide(env["gh"], NEW_HEAD, since)
    assert env["gh"].kinds()[-1] == "patch"  # 새 코멘트가 아니라 같은 코멘트를 고친다


def test_blocked_head_is_not_requeued_every_sync(env) -> None:
    env["gh"].head, env["gh"].author_association = NEW_HEAD, "NONE"
    _forbid_everything(env)
    main.handle(_update(NEW_HEAD), env["deps"])
    env["redis"].flushall()
    assert _sync_once(env).enqueued == []


def test_close_job_runs_without_the_check(env) -> None:
    """PR이 닫힐 때의 정리는 코드를 실행하지 않아 확인 없이 돈다."""
    env["gh"].state, env["gh"].author_association = "closed", "NONE"
    payload = PRJob(repo="o/r", pr_number=1, kind="close", source="sync").model_dump_json()
    assert main.handle(payload, env["deps"]) == "closed:0"


# --- 6-10: 판단은 워커가 GitHub에서 읽은 값으로 ---


def test_job_value_owner_does_not_override_lookup_none(env) -> None:
    env["gh"].author_association = "NONE"
    _forbid_everything(env)
    payload = _update(author_association="OWNER", author_login="outsider")
    assert main.handle(payload, env["deps"]) == guard.NEEDS_APPROVAL
    (run,) = _runs(env)
    assert (run.job_author_association, run.author_association) == ("OWNER", "NONE")


def test_lookup_member_runs_even_when_the_job_carries_nothing(env) -> None:
    env["gh"].author_association = "MEMBER"
    use_runner(env, lambda *a, **k: PASSED)
    assert main.handle(_update(), env["deps"]) == "passed"
    (run,) = _runs(env)
    assert (run.job_author_association, run.author_association) == (None, "MEMBER")


@pytest.mark.parametrize("payload", [_update(), _command("/fix"), _command("/recheck abc")])
def test_failed_lookup_runs_nothing(env, payload: str) -> None:
    env["gh"].head_error = "HTTP 502"
    _forbid_everything(env)
    with pytest.raises(GitHubError):
        main.handle(payload, env["deps"])
    assert _writes(env["gh"]) == [] and _runs(env) == []


def test_unreadable_lookup_is_a_failed_lookup(env) -> None:
    env["gh"].head = ""
    _forbid_everything(env)
    with pytest.raises(GitHubError):
        main.handle(_update(), env["deps"])
    assert _writes(env["gh"]) == [] and _runs(env) == []


def test_failed_base_settings_read_runs_nothing(env) -> None:
    def fail(*a, **k):
        raise GitError("fetch 실패")

    env["monkeypatch"].setattr(base_config, "load_access", fail)
    _forbid_everything(env)
    with pytest.raises(GitError):
        main.handle(_update(), env["deps"])
    assert _writes(env["gh"]) == [] and _runs(env) == []


# --- 6-8: base의 access 설정 ---


def test_trusted_list_from_base_settings_is_used(env) -> None:
    _access(env, AccessSettings(trusted=frozenset({"OWNER"})))
    env["gh"].author_association = "COLLABORATOR"
    _forbid_everything(env)
    assert main.handle(_update(), env["deps"]) == guard.NEEDS_APPROVAL


def test_listed_bot_runs(env) -> None:
    _access(env, AccessSettings(bots=frozenset({"dependabot[bot]"})))
    env["gh"].author_association, env["gh"].author_login = "NONE", "dependabot[bot]"
    use_runner(env, lambda *a, **k: PASSED)
    assert main.handle(_update(), env["deps"]) == "passed"


def test_listed_bot_cannot_approve(env) -> None:
    _access(env, AccessSettings(bots=frozenset({"dependabot[bot]"})))
    env["gh"].author_association = "NONE"
    _approver(env, login="dependabot[bot]", association="MEMBER")
    _forbid_everything(env)
    assert main.handle(_command(f"/recheck {HEAD}"), env["deps"]) == "approver_not_trusted"
    assert _writes(env["gh"]) == [] and not _approvals(env)


# --- 6-11: 커밋을 적은 /recheck 승인 ---


@pytest.mark.parametrize("written", [HEAD, HEAD.upper()])
def test_recheck_with_the_current_head_runs_that_commit_and_records_it(env, written: str) -> None:
    env["gh"].author_association = "NONE"
    _approver(env, body=f"/recheck {written}")
    use_runner(env, lambda *a, **k: PASSED)

    assert main.handle(_command(f"/recheck {written}"), env["deps"]) == "approved:passed"

    (approval,) = _approvals(env)
    assert (approval.repo, approval.pr_number, approval.head_sha) == ("o/r", 1, HEAD)
    assert (approval.approver, approval.comment_id) == ("maintainer", COMMENT_ID)
    assert approval.created_at is not None
    assert [(r.state, r.head_sha) for r in _runs(env)] == [("done", HEAD)]


@pytest.mark.parametrize(
    ("written", "result", "reply"),
    [
        ("/recheck", "approval_missing_commit", guard.missing_commit_reply),
        # 짧은 해시는 지금 head의 앞자리여도 승인이 아니다 (T31)
        (f"/recheck {HEAD[:7]}", "approval_bad_commit", guard.bad_commit_reply),
        (f"/recheck {HEAD[:39]}", "approval_bad_commit", guard.bad_commit_reply),
        ("/recheck please", "approval_bad_commit", guard.bad_commit_reply),
        (f"/recheck {NEW_HEAD}", "approval_head_moved", guard.head_moved_reply),
    ],
)
def test_recheck_that_does_not_name_the_head_runs_nothing_and_replies(
    env, written: str, result: str, reply
) -> None:
    env["gh"].author_association = "NONE"
    _approver(env, body=written)
    _forbid_everything(env)

    assert main.handle(_command(written), env["deps"]) == result

    writes = _writes(env["gh"])
    assert len(writes) == 1 and is_reply(writes[0][1])
    body = json.loads(writes[0][1])["body"]
    assert body.endswith(reply(HEAD))
    assert f"`/recheck {HEAD}`" in body  # 다시 달 명령은 전체 해시다
    assert not _approvals(env)
    assert env["deps"].store.command_handled(COMMENT_ID)


def test_reply_does_not_echo_what_the_comment_wrote(env) -> None:
    env["gh"].author_association = "NONE"
    _approver(env, body="/recheck @everyone</details>")
    _forbid_everything(env)
    main.handle(_command("/recheck @everyone</details>"), env["deps"])
    (writes,) = _writes(env["gh"])
    assert "@everyone" not in writes[1] and "</details>" not in writes[1]


def test_new_commit_after_approval_is_blocked_again(env) -> None:
    env["gh"].author_association = "NONE"
    _approver(env)
    use_runner(env, lambda *a, **k: PASSED)
    assert main.handle(_command(f"/recheck {HEAD}"), env["deps"]) == "approved:passed"
    # 승인한 커밋은 다시 넣어도 돈다
    assert main.handle(_update(), env["deps"]) == "passed"

    env["gh"].head = NEW_HEAD
    _forbid_everything(env)
    since = len(env["gh"].calls)
    assert main.handle(_update(NEW_HEAD), env["deps"]) == guard.NEEDS_APPROVAL
    _assert_only_the_guide(env["gh"], NEW_HEAD, since)
    # 옛 커밋을 적은 승인은 새 head에 쓰이지 않는다
    payload = _command(f"/recheck {HEAD}", comment_id=COMMENT_ID + 1)
    env["gh"].issue_comments[COMMENT_ID + 1] = env["gh"].issue_comments[COMMENT_ID]
    assert main.handle(payload, env["deps"]) == "approval_head_moved"
    assert [a.head_sha for a in _approvals(env)] == [HEAD]


def test_fix_on_an_approved_commit_is_not_blocked(env) -> None:
    env["gh"].author_association = "NONE"
    _approver(env)
    use_runner(env, lambda *a, **k: PASSED)
    main.handle(_command(f"/recheck {HEAD}"), env["deps"])
    result = main.handle(_command("/fix", comment_id=COMMENT_ID + 1), env["deps"])
    assert result == "no_pending_fix"  # 승인 안내가 아니라 /fix의 답이다


@pytest.mark.parametrize(
    ("login", "association", "pr"),
    [
        ("maintainer", "NONE", 1),  # 작업에는 OWNER로 실렸어도 지금 GitHub의 값은 NONE
        ("maintainer", "CONTRIBUTOR", 1),
        ("some-app[bot]", "OWNER", 1),  # 봇은 승인하지 못한다
        ("", "OWNER", 1),
        ("maintainer", "MEMBER", 2),  # 다른 PR의 코멘트
    ],
)
def test_approver_is_checked_against_github(env, login: str, association: str, pr: int) -> None:
    env["gh"].author_association = "NONE"
    _approver(env, login=login, association=association, pr=pr)
    _forbid_everything(env)
    assert main.handle(_command(f"/recheck {HEAD}"), env["deps"]) == "approver_not_trusted"
    assert _writes(env["gh"]) == [] and not _approvals(env)


def test_approver_must_be_in_base_trusted_list(env) -> None:
    _access(env, AccessSettings(trusted=frozenset({"OWNER"})))
    env["gh"].author_association = "NONE"
    _approver(env, association="MEMBER")
    _forbid_everything(env)
    assert main.handle(_command(f"/recheck {HEAD}"), env["deps"]) == "approver_not_trusted"


def test_edited_comment_is_read_from_github(env) -> None:
    """큐에서 기다리는 사이에 코멘트가 다른 글로 고쳐졌으면 승인으로 치지 않는다."""
    env["gh"].author_association = "NONE"
    _approver(env, body="고마워요")
    _forbid_everything(env)
    assert main.handle(_command(f"/recheck {HEAD}"), env["deps"]) == "approval_comment_changed"
    assert _writes(env["gh"]) == [] and not _approvals(env)


def test_failed_comment_lookup_runs_nothing(env) -> None:
    env["gh"].author_association = "NONE"  # 코멘트가 없어 조회가 실패한다
    _forbid_everything(env)
    with pytest.raises(GitHubError):
        main.handle(_command(f"/recheck {HEAD}"), env["deps"])
    assert _writes(env["gh"]) == [] and not _approvals(env)


def test_trusted_authors_recheck_runs_as_before(env) -> None:
    """신뢰된 작성자의 PR에서는 커밋 없는 `/recheck`가 지금처럼 head를 검사한다."""
    use_runner(env, lambda *a, **k: PASSED)
    assert main.handle(_command("/recheck"), env["deps"]) == "recheck:passed"
    assert not _approvals(env)
