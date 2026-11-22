"""app/worker/feedback.py: PR 코멘트 작업(feedback)과 머지 뒤 스레드 해결(resolve).

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
from app.worker import feedback, main, records_pure, retry
from app.worker.ai import claude_cli
from app.worker.ai.claude_cli import StructuredResult
from app.worker.feedback_triage_pure import Facts
from app.worker.github import GitHub, GitHubError
from app.worker.repo_config_pure import RepoConfig

from .helpers import HEAD
from .test_github import FakeGh

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


def _asker(answers: dict[int, tuple]):
    """판단 호출 자리에 넣는 함수. answers는 item_id → (verdict, why[, files, request, number])."""
    calls: list[str] = []

    def ask(prompt: str, **kw) -> StructuredResult:
        calls.append(prompt)
        items = [
            {"id": i, "verdict": a[0], "why": a[1], "files": list(a[2]) if len(a) > 2 else [],
             "request": a[3] if len(a) > 3 else "", "number": a[4] if len(a) > 4 else 0}
            for i, a in answers.items()
        ]  # fmt: skip
        return StructuredResult(ok=True, error=None, data={"items": items}, model_id="m",
                                input_tokens=1, output_tokens=1, cost_usd=0.0,
                                permission_denials=0)  # fmt: skip

    ask.calls = calls
    return ask


@pytest.fixture
def material(fb):
    """판단 자료 읽기(bare 캐시)를 바꿔 넣는다. 부른 횟수를 센다."""
    calls: list[int] = []

    def read(deps, work, items, ask):
        calls.append(len(items))
        return feedback.Material(RepoConfig(), (), "규칙 문서가 없음",
                                 Facts("제목", "", ("pr",), ("src/a.py",)), {})  # fmt: skip

    fb["monkeypatch"].setattr(feedback, "read_material", read)
    return calls


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
    items = feedback.collect(fb["deps"].github, store, "o/r", 1, HEAD)
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
    (item,) = feedback.collect(fb["deps"].github, fb["deps"].store, "o/r", 1, HEAD)
    assert (item.kind, item.item_id) == ("review_comment", 101)


# --- process_feedback -----------------------------------------------------------------


def test_feedback_with_items_judges_replies_and_records_each(fb, material) -> None:
    gh = fb["gh"]
    gh.threads = [_thread("T1", _comment(101))]
    gh.pr_comments = [_written(21, "좋네요", "owner", "OWNER")]
    ask = _asker({101: ("reject", "README 규칙과 어긋남"), 21: ("no_action", "칭찬")})
    fb["monkeypatch"].setattr(claude_cli, "run_structured", ask)
    assert feedback.process_feedback(_job(), fb["deps"]) == "feedback:no_action=1,rejected=1"
    assert len(ask.calls) == 1  # 남은 코멘트를 한 번에 묻는다
    ((where, body),) = gh.replies  # 할 일 없음에는 답글을 달지 않는다
    assert where == "101" and body.startswith(FEEDBACK_MARKER) and "반영하지 않습니다" in body
    rows = {f.item_id: f for f in fb["deps"].store.handled_feedback("o/r", 1)}
    assert (rows[101].verdict, rows[101].reply_id, rows[21].verdict) == (
        "rejected", 801, "no_action")  # fmt: skip
    # Copilot의 스레드는 거절도 바로 해결하고 사유를 준다. 할 일 없음은 스레드를 바꾸지 않는다
    assert gh.resolved == ["T1"] and gh.reasons == {"T1": "WONT_FIX"}
    assert rows[101].resolved_at is not None
    # 다시 불러도 기록한 코멘트는 다시 다루지 않는다(같은 답글을 두 번 달지 않는다)
    assert feedback.process_feedback(_job(), fb["deps"]) == "no_feedback"
    assert len(gh.replies) == 1


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


def test_handle_dispatches_feedback_and_releases_the_key(fb, material) -> None:
    fb["gh"].pr_comments = [_written(21, "일반", "owner", "OWNER")]
    fb["monkeypatch"].setattr(claude_cli, "run_structured", _asker({21: ("no_action", "질문")}))
    job = _job()
    fb["redis"].set(state_keys_pure.job(job.dedup_key), "1")
    assert main.handle(job.model_dump_json(), fb["deps"]) == "feedback:no_action=1"
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
    fb["monkeypatch"].setattr(feedback, "read_material", no_model)
    fb["monkeypatch"].setattr(feedback, "fix_accepted", no_model)


def test_comment_on_a_line_a_merged_proposal_fixed_is_resolved_without_the_model(fb) -> None:
    _fixed_by(fb, 900, "merged")
    _copilot_thread(fb, line=None, outdated=True)  # 머지로 줄이 밀려 낡았지만 처음 커밋의 줄이 같다
    _forbid_model(fb)
    assert feedback.process_feedback(_job(), fb["deps"]) == "feedback:merged=1"
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
    assert feedback.process_feedback(_job(), fb["deps"]) == "feedback:proposed=1"
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
    assert feedback.process_feedback(_job(), fb["deps"]) == "feedback:flagged=1"
    assert "규칙 검사가 같은 줄을 이미 위반으로 알렸습니다" in fb["gh"].replies[0][1]
    (row,) = fb["deps"].store.handled_feedback("o/r", 1)
    assert (row.verdict, row.why) == ("no_action", "규칙 검사가 같은 줄을 이미 위반으로 알림")


def test_lines_from_another_commit_are_left_to_the_model(fb, material) -> None:
    """수정안을 만든 커밋과 코멘트를 단 커밋이 다르고 스레드가 낡았으면 줄을 맞출 수 없다."""
    _fixed_by(fb, 900, "merged", head="e" * 40)
    _copilot_thread(fb, commit="f" * 40, line=None, outdated=True)
    fb["monkeypatch"].setattr(
        claude_cli,
        "run_structured",
        _asker({101: ("already_fixed", "a.py가 이미 0을 막음", [], "", 900)}),
    )
    assert feedback.process_feedback(_job(), fb["deps"]) == "feedback:already_fixed=1"
    assert material == [1]
    assert "지금 코드에 이미 반영돼 있습니다" in fb["gh"].replies[0][1]
    assert fb["gh"].resolved == ["T1"]
    (row,) = fb["deps"].store.handled_feedback("o/r", 1)
    assert (row.verdict, row.proposal_number) == ("accepted", 900)
    assert row.resolved_at is not None


@pytest.mark.parametrize(("result", "outcome", "verdict", "number", "text"), [
    (feedback.FixResult("fixed", number=905), "fixed", "accepted", 905,
     "반영했습니다. 수정안 #905"),
    (feedback.FixResult("failed", reason="고친 뒤 테스트: 1 failed"), "failed", "error", None,
     "검증을 통과한 수정을 만들지 못했습니다"),
    (feedback.FixResult("deferred", reason="테스트가 실패함"), "deferred", "deferred", None,
     "지금은 고칠 수 없습니다"),
])  # fmt: skip
def test_accepted_comments_are_fixed_in_one_proposal(
    fb, material, result, outcome, verdict, number, text
) -> None:
    _copilot_thread(fb)
    fb["gh"].review_bodies = [_written(11, "여기도 0을 막아 주세요")]
    fb["monkeypatch"].setattr(claude_cli, "run_structured", _asker({
        101: ("accept", "0으로 나누기", ["src/a.py"], "b가 0이면 ValueError"),
        11: ("accept", "같은 결함", ["src/a.py"], "b가 0이면 ValueError"),
    }))  # fmt: skip
    got: list[list[int]] = []

    def fix(work, deps, refs, accepted):
        got.append([a.item.item_id for a in accepted])
        return result

    fb["monkeypatch"].setattr(feedback, "fix_accepted", fix)
    assert feedback.process_feedback(_job(), fb["deps"]) == f"feedback:{outcome}=2"
    assert got == [[101, 11]]  # 수정안은 하나
    assert [w for w, _ in fb["gh"].replies] == ["101", "issue"]
    assert all(text in b for _, b in fb["gh"].replies)
    assert "`kim`의 리뷰(id 11)에 답합니다." in fb["gh"].replies[1][1]
    rows = fb["deps"].store.handled_feedback("o/r", 1)
    assert [(r.verdict, r.proposal_number) for r in rows] == [(verdict, number)] * 2


def test_review_body_without_files_falls_back_to_error(fb, material) -> None:
    """리뷰 본문은 달린 파일이 없다. 고칠 파일을 답하지 않은 accept는 error다."""
    fb["gh"].review_bodies = [_written(11, "전체적으로 고쳐 주세요")]
    fb["monkeypatch"].setattr(
        claude_cli, "run_structured", _asker({11: ("accept", "?", [], "고친다")})
    )
    assert feedback.process_feedback(_job(), fb["deps"]) == "feedback:error=1"
    assert fb["gh"].replies == []  # 판단하지 못한 것에는 답글을 달지 않는다


def test_model_failure_records_error_and_new_commit_revisits(fb, material) -> None:
    fb["gh"].pr_comments = [_written(21, "고쳐 주세요", "owner", "OWNER")]

    def broken(prompt, **kw):
        return StructuredResult(ok=False, error="timeout", data=None, model_id="",
                                input_tokens=None, output_tokens=None, cost_usd=None,
                                permission_denials=0)  # fmt: skip

    fb["monkeypatch"].setattr(claude_cli, "run_structured", broken)
    assert feedback.process_feedback(_job(), fb["deps"]) == "feedback:error=1"
    store = fb["deps"].store
    assert feedback.collect(fb["deps"].github, store, "o/r", 1, HEAD) == []
    assert len(feedback.collect(fb["deps"].github, store, "o/r", 1, "d" * 40)) == 1


def test_triage_answer_is_stored_and_not_asked_again(fb, material) -> None:
    fb["gh"].pr_comments = [_written(21, "질문", "owner", "OWNER")]
    ask = _asker({21: ("no_action", "질문")})
    fb["monkeypatch"].setattr(claude_cli, "run_structured", ask)
    item = feedback.collect(fb["deps"].github, fb["deps"].store, "o/r", 1, HEAD)
    refs = fb["deps"].github.pr_refs("o/r", 1)
    feedback.handle_items(_job(), fb["deps"], refs, item)
    feedback.handle_items(_job(), fb["deps"], refs, item)
    assert len(ask.calls) == 1
    stages = [(c.stage, c.cache_hit) for c in fb["deps"].store.model_calls("o/r", 1)]
    assert stages == [("feedback", False), ("feedback", True)]


def test_a_merge_the_webhook_missed_is_seen_before_masking(fb) -> None:
    """MathLLM#7: 규칙 검사의 수정안이 같은 줄을 고쳐 머지됐는데 그 스레드가 열린 채 남았다."""
    _fixed_by(fb, 900, "open")
    fb["gh"].pulls[900] = {"state": "closed", "merged": True, "merge_commit": "c" * 40}
    _copilot_thread(fb)
    _forbid_model(fb)
    assert feedback.process_feedback(_job(), fb["deps"]) == "feedback:merged=1"
    assert fb["gh"].resolved == ["T1"]


def test_rejecting_resolves_copilot_threads_with_a_reason_and_leaves_human_threads_open(
    fb, material
) -> None:
    """Copilot의 스레드는 거절의 종류대로 사유를 주고 해결한다(Incorrect → INVALID). 사람의 스레드는
    답글만 달고 열어 둔다."""
    gh = fb["gh"]
    gh.threads = [
        _thread("T1", _comment(101)),
        _thread("T2", _comment(102, login="kim", assoc="MEMBER")),
    ]

    def ask(prompt: str, **kw) -> StructuredResult:
        items = [
            {"id": i, "verdict": "reject", "why": "코드를 읽어 보니 그 문제가 없다", "files": [],
             "request": "", "number": 0, "reject_kind": "incorrect"}
            for i in (101, 102)
        ]  # fmt: skip
        return StructuredResult(ok=True, error=None, data={"items": items}, model_id="m",
                                input_tokens=1, output_tokens=1, cost_usd=0.0,
                                permission_denials=0)  # fmt: skip

    fb["monkeypatch"].setattr(claude_cli, "run_structured", ask)
    assert feedback.process_feedback(_job(), fb["deps"]) == "feedback:rejected=2"
    assert gh.resolved == ["T1"] and gh.reasons == {"T1": "INVALID"}
    assert len(gh.replies) == 2
    rows = {f.item_id: f for f in fb["deps"].store.handled_feedback("o/r", 1)}
    assert rows[101].resolved_at is not None and rows[102].resolved_at is None
