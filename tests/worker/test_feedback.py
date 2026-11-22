"""app/worker/feedback.py: PR 코멘트 작업(feedback)과 머지 뒤 스레드 해결(resolve). 모델은 없다."""

import json
from typing import Final

import pytest

from app.shared import state_keys_pure
from app.shared.feedback_pure import FEEDBACK_MARKER
from app.shared.job_pure import PRJob
from app.worker import feedback, main, retry
from app.worker.github import GitHub, GitHubError

from .helpers import HEAD
from .test_github import FakeGh

COPILOT: Final = "copilot-pull-request-reviewer"


def _comment(cid: int, body: str = "위험합니다", login: str = COPILOT, assoc: str = "NONE") -> dict:
    return {"databaseId": cid, "author": {"login": login}, "authorAssociation": assoc,
            "body": body}  # fmt: skip


def _thread(tid: str, *comments: dict, resolved: bool = False, outdated: bool = False) -> dict:
    return {"id": tid, "isResolved": resolved, "isOutdated": outdated, "path": "src/a.py",
            "line": 3, "comments": {"nodes": list(comments)}}  # fmt: skip


class FeedbackGh(FakeGh):
    """FakeGh에 스레드(GraphQL)·리뷰 본문·일반 코멘트 읽기와 스레드 해결을 더한다."""

    def __init__(self) -> None:
        super().__init__()
        self.threads: list[dict] = []
        self.review_bodies: list[dict] = []
        self.pr_comments: list[dict] = []
        self.resolved: list[str] = []
        self.resolve_error: str | None = None

    def __call__(self, args: list[str], stdin: str | None) -> str:
        if args[:2] == ["api", "graphql"]:
            self.calls.append((args, stdin))
            query = next(a for a in args if a.startswith("query="))
            if "resolveReviewThread" in query:
                if self.resolve_error:
                    raise GitHubError(self.resolve_error)
                self.resolved.append(next(a[3:] for a in args if a.startswith("id=")))
                return json.dumps({"data": {"resolveReviewThread": {}}})
            nodes = {"nodes": self.threads}
            return json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": nodes}}}})
        rest = next((a for a in args if a.startswith("repos/o/r/")), "")
        if "--paginate" in args and "association:" in args[args.index("--jq") + 1]:
            self.calls.append((args, stdin))
            items = {
                "repos/o/r/pulls/1/reviews": self.review_bodies,
                "repos/o/r/issues/1/comments": self.pr_comments,
            }[rest]
            return "".join(json.dumps(i) + "\n" for i in items)
        return super().__call__(args, stdin)

    def writes(self) -> list[list[str]]:
        """읽기가 아닌 호출 (POST·PATCH·DELETE·GraphQL mutation)."""
        return [
            args for args, _ in self.calls
            if any(m in args for m in ("-X", "--input")) or any("mutation" in a for a in args)
        ]  # fmt: skip


@pytest.fixture
def fb(env):
    gh = FeedbackGh()
    env["deps"].github = GitHub(gh)
    env["gh"] = gh
    return env


def _job(kind: str = "feedback", **extra) -> PRJob:
    fields = {"repo": "o/r", "pr_number": 1, "kind": kind, "source": "webhook"} | extra
    if kind == "feedback":
        fields.setdefault("head_sha", HEAD)
    return PRJob(**fields)


def _written(cid: int, body: str = "본문", login: str = "kim", assoc: str = "MEMBER") -> dict:
    return {"id": cid, "login": login, "association": assoc, "body": body}


# --- collect --------------------------------------------------------------------------


def test_collect_reads_three_places_and_keeps_what_is_handled(fb) -> None:
    gh = fb["gh"]
    gh.threads = [
        _thread("T1", _comment(101), _comment(102, "동의", "kim", "MEMBER"),
                _comment(103, f"{FEEDBACK_MARKER}\n답", "owner", "OWNER")),
        _thread("T2", _comment(201), resolved=True),  # 이미 해결됨
        _thread("T3", _comment(301, "명령", "owner", "OWNER").copy() | {"body": "/fix"}),
        _thread("T4", _comment(401, login="outsider", assoc="CONTRIBUTOR")),
        _thread("T5", _comment(501, login="Copilot", assoc="NONE"), outdated=True),
    ]  # fmt: skip
    gh.review_bodies = [_written(11, "리뷰 본문"), _written(12, "처리함")]
    gh.pr_comments = [
        _written(21, "일반 코멘트", "owner", "OWNER"),
        _written(22, f"{FEEDBACK_MARKER}\n파이프라인", "owner", "OWNER"),
        _written(23, "<!-- pr-proof -->\n결과", "owner", "OWNER"),
        _written(24, "/recheck", "owner", "OWNER"),
        _written(25, "외부인", "stranger", "NONE"),
    ]
    store = fb["deps"].store
    store.record_feedback(repo="o/r", pr_number=1, kind="review", item_id=12, thread_id=None,
                          login="kim", head_sha=HEAD, verdict="rejected")  # fmt: skip
    items = feedback.collect(fb["deps"].github, store, "o/r", 1)
    assert [(i.kind, i.item_id) for i in items] == [
        ("review_comment", 101), ("review_comment", 501), ("review", 11), ("issue_comment", 21),
    ]  # fmt: skip
    first = items[0]
    assert (first.thread_id, first.path, first.line, first.replies) == (
        "T1",
        "src/a.py",
        3,
        ("동의",),
    )
    assert items[1].outdated is True
    assert items[2].thread_id is None and items[2].path == ""


def test_collect_uses_the_same_kind_and_id_as_the_sync(fb) -> None:
    """동기화는 줄 코멘트의 답글을 빼고 스레드의 첫 코멘트 id를 쓴다. 키가 같아야 처리한 코멘트를
    동기화가 다시 넣지 않는다."""
    fb["gh"].threads = [_thread("T1", _comment(101), _comment(102, "답", "kim", "MEMBER"))]
    (item,) = feedback.collect(fb["deps"].github, fb["deps"].store, "o/r", 1)
    assert (item.kind, item.item_id) == ("review_comment", 101)


# --- process_feedback -----------------------------------------------------------------


def test_feedback_with_items_is_pending_and_leaves_no_trace(fb) -> None:
    """모델 판단과 수정은 다음 작업이 넣는다. 기록을 남기면 그 코멘트를 다시 보지 못한다."""
    gh = fb["gh"]
    gh.threads = [_thread("T1", _comment(101))]
    gh.pr_comments = [_written(21, "일반", "owner", "OWNER")]
    assert feedback.process_feedback(_job(), fb["deps"]) == "feedback:pending:2"
    assert gh.writes() == []  # 답글도 코멘트도 달지 않는다
    assert fb["deps"].store.handled_feedback("o/r", 1) == []
    # 다시 불러도 같은 코멘트를 준다
    assert feedback.process_feedback(_job(), fb["deps"]) == "feedback:pending:2"


def test_feedback_without_items_is_no_feedback(fb) -> None:
    fb["gh"].threads = [_thread("T1", _comment(101), resolved=True)]
    assert feedback.process_feedback(_job(), fb["deps"]) == "no_feedback"
    assert fb["gh"].writes() == []


def test_feedback_on_closed_pr_does_nothing(fb) -> None:
    fb["gh"].state = "closed"
    fb["gh"].threads = [_thread("T1", _comment(101))]
    assert feedback.process_feedback(_job(), fb["deps"]) == "closed"
    assert not [
        a for a, _ in fb["gh"].calls if a[:2] == ["api", "graphql"]
    ]  # 코멘트를 읽지도 않는다
    latest = fb["redis"].get(state_keys_pure.latest_head("o/r", 1))
    assert latest.decode() == "closed"


def test_feedback_on_untrusted_authors_pr_only_leaves_the_approval_guide(fb) -> None:
    fb["gh"].author_association = "NONE"
    fb["gh"].threads = [_thread("T1", _comment(101))]
    assert feedback.process_feedback(_job(), fb["deps"]) == "needs_approval"
    assert not [a for a, _ in fb["gh"].calls if a[:2] == ["api", "graphql"]]
    assert fb["deps"].store.handled_feedback("o/r", 1) == []


@pytest.mark.parametrize("final", [False, True])
def test_feedback_read_failure_is_retried_and_last_attempt_releases_the_key(
    fb, final: bool
) -> None:
    fb["monkeypatch"].setattr(retry, "attempt", lambda: (4 if final else 1, final))
    fb["monkeypatch"].setattr(
        feedback, "collect", lambda *a, **k: (_ for _ in ()).throw(GitHubError("502"))
    )
    job = _job()
    fb["redis"].set(state_keys_pure.job(job.dedup_key), "1")
    with pytest.raises(GitHubError):
        feedback.process_feedback(job, fb["deps"])
    assert (fb["redis"].get(state_keys_pure.job(job.dedup_key)) is None) == final


def test_handle_dispatches_feedback_and_releases_the_key(fb) -> None:
    fb["gh"].pr_comments = [_written(21, "일반", "owner", "OWNER")]
    job = _job()
    fb["redis"].set(state_keys_pure.job(job.dedup_key), "1")
    assert main.handle(job.model_dump_json(), fb["deps"]) == "feedback:pending:1"
    assert fb["redis"].get(state_keys_pure.job(job.dedup_key)) is None


# --- process_resolve ------------------------------------------------------------------


def _accepted(store, item_id: int, thread: str | None, proposal: int) -> None:
    store.record_feedback(
        repo="o/r", pr_number=1, kind="review_comment", item_id=item_id, thread_id=thread,
        login=COPILOT, head_sha=HEAD, verdict="accepted", proposal_number=proposal,
    )  # fmt: skip


def _proposal(fb, number: int, *, state: str, merged: bool = False, bundle: str = "model") -> None:
    fb["deps"].store.record_proposal("o/r", 1, bundle, HEAD, f"pipeline/pr-1-fix-{bundle}", number)
    fb["gh"].pulls[number] = {"state": state, "merged": merged,
                              "merge_commit": "c" * 40 if merged else ""}  # fmt: skip


def test_resolve_changes_only_threads_of_merged_proposals(fb) -> None:
    store = fb["deps"].store
    _accepted(store, 101, "T1", 900)  # 머지됨
    _accepted(store, 102, "T2", 900)  # 같은 수정안의 다른 스레드
    _accepted(store, 103, "T3", 901)  # 머지하지 않고 닫음
    _accepted(store, 104, "T4", 902)  # 아직 열려 있음
    _proposal(fb, 900, state="closed", merged=True, bundle="model")
    # 같은 묶음의 수정안은 열린 것이 하나라 다른 묶음 이름으로 기록한다
    _proposal(fb, 901, state="closed", merged=False, bundle="mechanical")
    store.record_proposal("o/r", 1, "other", HEAD, "pipeline/pr-1-fix-other", 902)
    fb["gh"].pulls[902] = {"state": "open", "merged": False, "merge_commit": ""}

    assert feedback.process_resolve(_job("resolve"), fb["deps"]) == "resolved:2"
    assert sorted(fb["gh"].resolved) == ["T1", "T2"]
    rows = {f.item_id: f for f in store.handled_feedback("o/r", 1)}
    assert rows[101].resolved_at is not None and rows[102].resolved_at is not None
    assert rows[103].resolved_at is None and rows[104].resolved_at is None
    assert [p.state for p in store.proposals("o/r", 1)] == ["merged", "rejected", "open"]


def test_resolve_does_not_touch_threads_when_the_proposal_was_closed_without_merge(fb) -> None:
    _accepted(fb["deps"].store, 101, "T1", 900)
    _proposal(fb, 900, state="closed", merged=False)
    assert feedback.process_resolve(_job("resolve"), fb["deps"]) == "resolved:0"
    assert fb["gh"].resolved == []
    assert [f.resolved_at for f in fb["deps"].store.handled_feedback("o/r", 1)] == [None]


def test_resolve_twice_resolves_once(fb) -> None:
    _accepted(fb["deps"].store, 101, "T1", 900)
    _proposal(fb, 900, state="closed", merged=True)
    assert feedback.process_resolve(_job("resolve"), fb["deps"]) == "resolved:1"
    assert feedback.process_resolve(_job("resolve"), fb["deps"]) == "resolved:0"
    assert fb["gh"].resolved == ["T1"]


def test_resolve_ignores_rows_without_thread_or_not_accepted(fb) -> None:
    store = fb["deps"].store
    _accepted(store, 11, None, 900)  # 리뷰 본문·일반 코멘트는 스레드가 없다
    store.record_feedback(repo="o/r", pr_number=1, kind="review_comment", item_id=12,
                          thread_id="T2", login=COPILOT, head_sha=HEAD,
                          verdict="rejected")  # fmt: skip
    _proposal(fb, 900, state="closed", merged=True)
    assert feedback.process_resolve(_job("resolve"), fb["deps"]) == "resolved:0"
    assert fb["gh"].resolved == []


def test_resolve_works_for_a_closed_original_pr(fb) -> None:
    """수정안을 머지한 뒤 원래 PR도 머지됐을 수 있다. 원래 PR의 상태는 보지 않는다."""
    fb["gh"].state = "closed"
    _accepted(fb["deps"].store, 101, "T1", 900)
    _proposal(fb, 900, state="closed", merged=True)
    assert feedback.process_resolve(_job("resolve"), fb["deps"]) == "resolved:1"


def test_resolve_failure_keeps_progress_and_retries(fb) -> None:
    store = fb["deps"].store
    _accepted(store, 101, "T1", 900)
    _accepted(store, 102, "T2", 900)
    _proposal(fb, 900, state="closed", merged=True)
    fb["gh"].resolve_error = "HTTP 502"
    fb["monkeypatch"].setattr(retry, "attempt", lambda: (1, False))
    with pytest.raises(GitHubError):
        feedback.process_resolve(_job("resolve"), fb["deps"])
    assert [f.resolved_at for f in store.handled_feedback("o/r", 1)] == [None, None]
    fb["gh"].resolve_error = None
    assert feedback.process_resolve(_job("resolve"), fb["deps"]) == "resolved:2"
    assert all(f.resolved_at is not None for f in store.handled_feedback("o/r", 1))


def test_handle_dispatches_resolve_and_releases_the_key(fb) -> None:
    _accepted(fb["deps"].store, 101, "T1", 900)
    _proposal(fb, 900, state="closed", merged=True)
    job = _job("resolve")
    fb["redis"].set(state_keys_pure.job(job.dedup_key), "1")
    assert main.handle(job.model_dump_json(), fb["deps"]) == "resolved:1"
    assert fb["redis"].get(state_keys_pure.job(job.dedup_key)) is None
