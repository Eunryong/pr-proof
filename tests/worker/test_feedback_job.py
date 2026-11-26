"""app/worker/feedback_job.py: PR 코멘트 작업(feedback)과 머지 뒤 스레드 해결(resolve).

모델은 부르지 않는다. 판단 호출 자리(ask)에는 정해진 답을 돌려주는 함수를 넣고, 판단 자료 읽기
(`read_material`: bare 캐시)와 수정(`fix_accepted`: 체크아웃·샌드박스·수정 작성)은 바꿔 넣는다.
그 둘을 실제로 도는 것은 tests/e2e/test_feedback_cases.py다.
"""

import json
from typing import Final

import pytest

from app.shared import state_keys_pure
from app.shared.db_pure import FixAttempt, SeenViolation
from app.shared.feedback_pure import FEEDBACK_MARKER
from app.shared.job_pure import PRJob
from app.worker import feedback_job, main, retry
from app.worker.git.github import GitHub, GitHubError
from app.worker.model.claude import claude_cli
from app.worker.store import records_pure

from .git.test_github import FakeGh
from .helpers import HEAD

COPILOT: Final = "copilot-pull-request-reviewer"


def _comment(cid: int, body: str = "위험합니다", login: str = COPILOT, assoc: str = "NONE") -> dict:
    return {"databaseId": cid, "author": {"login": login}, "authorAssociation": assoc,
            "body": body}  # fmt: skip


def _thread(tid: str, *comments: dict, resolved: bool = False, outdated: bool = False,
            line: int | None = 3, original_line: int | None = 3) -> dict:  # fmt: skip
    return {"id": tid, "isResolved": resolved, "isOutdated": outdated, "path": "src/a.py",
            "line": line, "originalLine": original_line,
            "comments": {"nodes": list(comments)}}  # fmt: skip


class FeedbackGh(FakeGh):
    """FakeGh에 스레드(GraphQL)·리뷰 본문·일반 코멘트 읽기와 스레드 해결을 더한다."""

    def __init__(self) -> None:
        super().__init__()
        self.threads: list[dict] = []
        self.review_bodies: list[dict] = []
        self.pr_comments: list[dict] = []
        self.resolved: list[str] = []
        self.reasons: dict[str, str | None] = {}  # 스레드 id → 해결 사유 (없으면 None)
        self.resolve_error: str | None = None
        self.replies: list[tuple[str, str]] = []  # (어디에, 본문): 줄 코멘트 id 또는 "issue"

    def __call__(self, args: list[str], stdin: str | None) -> str:
        target = next((a for a in args if a.endswith("/replies")), "")
        if target:
            self.calls.append((args, stdin))
            self.replies.append((target.split("/")[-2], json.loads(stdin)["body"]))
            return f"{800 + len(self.replies)}\n"
        if "repos/o/r/issues/1/comments" in args and "--input" in args:
            self.calls.append((args, stdin))
            self.replies.append(("issue", json.loads(stdin)["body"]))
            return f"{800 + len(self.replies)}\n"
        if args[:2] == ["api", "graphql"]:
            self.calls.append((args, stdin))
            query = next(a for a in args if a.startswith("query="))
            if "resolveReviewThread" in query:
                if self.resolve_error:
                    raise GitHubError(self.resolve_error)
                tid = next(a[3:] for a in args if a.startswith("id="))
                self.resolved.append(tid)
                self.reasons[tid] = next((a[7:] for a in args if a.startswith("reason=")), None)
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
    items = feedback_job.collect(fb["deps"].github, store, "o/r", 1, HEAD)
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
    (item,) = feedback_job.collect(fb["deps"].github, fb["deps"].store, "o/r", 1, HEAD)
    assert (item.kind, item.item_id) == ("review_comment", 101)


# --- process_feedback -----------------------------------------------------------------


def test_feedback_without_items_is_no_feedback(fb) -> None:
    fb["gh"].threads = [_thread("T1", _comment(101), resolved=True)]
    assert feedback_job.process_feedback(_job(), fb["deps"]) == "no_feedback"
    assert fb["gh"].writes() == []


def test_feedback_on_closed_pr_does_nothing(fb) -> None:
    fb["gh"].state = "closed"
    fb["gh"].threads = [_thread("T1", _comment(101))]
    assert feedback_job.process_feedback(_job(), fb["deps"]) == "closed"
    assert not [
        a for a, _ in fb["gh"].calls if a[:2] == ["api", "graphql"]
    ]  # 코멘트를 읽지도 않는다
    latest = fb["redis"].get(state_keys_pure.latest_head("o/r", 1))
    assert latest.decode() == "closed"


def test_feedback_on_untrusted_authors_pr_only_leaves_the_approval_guide(fb) -> None:
    fb["gh"].author_association = "NONE"
    fb["gh"].threads = [_thread("T1", _comment(101))]
    assert feedback_job.process_feedback(_job(), fb["deps"]) == "needs_approval"
    assert not [a for a, _ in fb["gh"].calls if a[:2] == ["api", "graphql"]]
    assert fb["deps"].store.handled_feedback("o/r", 1) == []


@pytest.mark.parametrize("final", [False, True])
def test_feedback_read_failure_is_retried_and_last_attempt_releases_the_key(
    fb, final: bool
) -> None:
    fb["monkeypatch"].setattr(retry, "attempt", lambda: (4 if final else 1, final))
    fb["monkeypatch"].setattr(
        feedback_job, "collect", lambda *a, **k: (_ for _ in ()).throw(GitHubError("502"))
    )
    job = _job()
    fb["redis"].set(state_keys_pure.job(job.dedup_key), "1")
    with pytest.raises(GitHubError):
        feedback_job.process_feedback(job, fb["deps"])
    assert (fb["redis"].get(state_keys_pure.job(job.dedup_key)) is None) == final


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

    assert feedback_job.process_resolve(_job("resolve"), fb["deps"]) == "resolved:2"
    assert sorted(fb["gh"].resolved) == ["T1", "T2"]
    rows = {f.item_id: f for f in store.handled_feedback("o/r", 1)}
    assert rows[101].resolved_at is not None and rows[102].resolved_at is not None
    assert rows[103].resolved_at is None and rows[104].resolved_at is None
    assert [p.state for p in store.proposals("o/r", 1)] == ["merged", "rejected", "open"]


def test_resolve_does_not_touch_threads_when_the_proposal_was_closed_without_merge(fb) -> None:
    _accepted(fb["deps"].store, 101, "T1", 900)
    _proposal(fb, 900, state="closed", merged=False)
    assert feedback_job.process_resolve(_job("resolve"), fb["deps"]) == "resolved:0"
    assert fb["gh"].resolved == []
    assert [f.resolved_at for f in fb["deps"].store.handled_feedback("o/r", 1)] == [None]


def test_resolve_twice_resolves_once(fb) -> None:
    _accepted(fb["deps"].store, 101, "T1", 900)
    _proposal(fb, 900, state="closed", merged=True)
    assert feedback_job.process_resolve(_job("resolve"), fb["deps"]) == "resolved:1"
    assert feedback_job.process_resolve(_job("resolve"), fb["deps"]) == "resolved:0"
    assert fb["gh"].resolved == ["T1"]


def test_resolve_ignores_rows_without_thread_or_not_accepted(fb) -> None:
    store = fb["deps"].store
    _accepted(store, 11, None, 900)  # 리뷰 본문·일반 코멘트는 스레드가 없다
    store.record_feedback(repo="o/r", pr_number=1, kind="review_comment", item_id=12,
                          thread_id="T2", login=COPILOT, head_sha=HEAD,
                          verdict="rejected")  # fmt: skip
    _proposal(fb, 900, state="closed", merged=True)
    assert feedback_job.process_resolve(_job("resolve"), fb["deps"]) == "resolved:0"
    assert fb["gh"].resolved == []


def test_resolve_works_for_a_closed_original_pr(fb) -> None:
    """수정안을 머지한 뒤 원래 PR도 머지됐을 수 있다. 원래 PR의 상태는 보지 않는다."""
    fb["gh"].state = "closed"
    _accepted(fb["deps"].store, 101, "T1", 900)
    _proposal(fb, 900, state="closed", merged=True)
    assert feedback_job.process_resolve(_job("resolve"), fb["deps"]) == "resolved:1"


def test_resolve_failure_keeps_progress_and_retries(fb) -> None:
    store = fb["deps"].store
    _accepted(store, 101, "T1", 900)
    _accepted(store, 102, "T2", 900)
    _proposal(fb, 900, state="closed", merged=True)
    fb["gh"].resolve_error = "HTTP 502"
    fb["monkeypatch"].setattr(retry, "attempt", lambda: (1, False))
    with pytest.raises(GitHubError):
        feedback_job.process_resolve(_job("resolve"), fb["deps"])
    assert [f.resolved_at for f in store.handled_feedback("o/r", 1)] == [None, None]
    fb["gh"].resolve_error = None
    assert feedback_job.process_resolve(_job("resolve"), fb["deps"]) == "resolved:2"
    assert all(f.resolved_at is not None for f in store.handled_feedback("o/r", 1))


def test_handle_dispatches_resolve_and_releases_the_key(fb) -> None:
    _accepted(fb["deps"].store, 101, "T1", 900)
    _proposal(fb, 900, state="closed", merged=True)
    job = _job("resolve")
    fb["redis"].set(state_keys_pure.job(job.dedup_key), "1")
    assert main.handle(job.model_dump_json(), fb["deps"]) == "resolved:1"
    assert fb["redis"].get(state_keys_pure.job(job.dedup_key)) is None


# --- handle_items: 겹침, 판단, 수정 --------------------------------------------------


def _fixed_by(fb, number: int, state: str, *, line: int = 3, head: str = HEAD) -> None:
    """수정안(번호, 상태)과 그 시도가 고친 대상 위반 src/a.py:line을 기록한다."""
    store = fb["deps"].store
    target = records_pure.Target("v1", "R-doc-1", "규칙", "src/a.py", line, "x = 1", "")
    attempt = store.save_fix_attempt(FixAttempt(
        repo="o/r", pr_number=1, head_sha=head, merge_base="b" * 40, bundle="model",
        status="ready", attempts=1, models_json="[]", detail="",
        targets_json=records_pure.targets_to_json([target]), patch="p",
    ))  # fmt: skip
    store.record_proposal("o/r", 1, f"model{number}", head, "br", number, attempt_id=attempt)
    fb["gh"].pulls[number] = {"state": "open", "merged": False, "merge_commit": ""}
    if state != "open":
        (row,) = [p for p in store.proposals("o/r", 1) if p.number == number]
        store.set_proposal_state(row.id, state, merge_commit="c" * 40)


def _copilot_thread(fb, *, commit: str = HEAD, line: int | None = 3, outdated=False) -> None:
    comment = _comment(101) | {"originalCommit": {"oid": commit}}
    fb["gh"].threads = [_thread("T1", comment, line=line, outdated=outdated)]


def _forbid_model(fb) -> None:
    def no_model(*a, **k):
        raise AssertionError("모델을 부르면 안 된다")

    fb["monkeypatch"].setattr(claude_cli, "run_structured", no_model)
    fb["monkeypatch"].setattr(feedback_job, "read_material", no_model)
    fb["monkeypatch"].setattr(feedback_job, "fix_accepted", no_model)


def test_comment_on_a_line_a_merged_proposal_fixed_is_resolved_without_the_model(fb) -> None:
    _fixed_by(fb, 900, "merged")
    _copilot_thread(fb, line=None, outdated=True)  # 머지로 줄이 밀려 낡았지만 처음 커밋의 줄이 같다
    _forbid_model(fb)
    assert feedback_job.process_feedback(_job(), fb["deps"]) == "feedback:merged=1"
    ((where, body),) = fb["gh"].replies
    assert where == "101" and "수정안 #900에서 고쳤습니다(머지됨)" in body
    assert fb["gh"].resolved == ["T1"]
    (row,) = fb["deps"].store.handled_feedback("o/r", 1)
    assert (row.verdict, row.proposal_number) == ("accepted", 900)
    assert row.resolved_at is not None


def test_comment_on_a_line_an_open_proposal_fixes_waits_for_the_merge(fb) -> None:
    _fixed_by(fb, 901, "open")
    _copilot_thread(fb)
    _forbid_model(fb)
    assert feedback_job.process_feedback(_job(), fb["deps"]) == "feedback:proposed=1"
    assert "수정안 #901이 이미 고칩니다" in fb["gh"].replies[0][1]
    assert fb["gh"].resolved == []  # 머지된 뒤 resolve 작업이 바꾼다
    (row,) = fb["deps"].store.handled_feedback("o/r", 1)
    assert (row.verdict, row.proposal_number, row.resolved_at) == ("accepted", 901, None)
    assert [r.item_id for r in fb["deps"].store.feedback_awaiting_merge("o/r", 1)] == [101]


def test_comment_on_a_line_the_rules_check_flagged_is_not_fixed_twice(fb) -> None:
    fb["deps"].store.replace_violations("o/r", 1, HEAD, [SeenViolation(
        repo="o/r", pr_number=1, head_sha=HEAD, violation_id="v", rule_id="R", file="src/a.py",
        line=3, message="m", unit="line",
    )])  # fmt: skip
    _copilot_thread(fb)
    _forbid_model(fb)
    assert feedback_job.process_feedback(_job(), fb["deps"]) == "feedback:flagged=1"
    assert "규칙 검사가 같은 줄을 이미 위반으로 알렸습니다" in fb["gh"].replies[0][1]
    (row,) = fb["deps"].store.handled_feedback("o/r", 1)
    assert (row.verdict, row.why) == ("no_action", "규칙 검사가 같은 줄을 이미 위반으로 알림")


def test_a_merge_the_webhook_missed_is_seen_before_masking(fb) -> None:
    """MathLLM#7: 규칙 검사의 수정안이 같은 줄을 고쳐 머지됐는데 그 스레드가 열린 채 남았다."""
    _fixed_by(fb, 900, "open")
    fb["gh"].pulls[900] = {"state": "closed", "merged": True, "merge_commit": "c" * 40}
    _copilot_thread(fb)
    _forbid_model(fb)
    assert feedback_job.process_feedback(_job(), fb["deps"]) == "feedback:merged=1"
    assert fb["gh"].resolved == ["T1"]
