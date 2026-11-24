"""같은 저장소를 보는 다른 서버: 워커가 결과 코멘트의 표시를 읽어 검사할지 정하는 흐름.

GitHub은 FakeGh가 대신한다. 결과 코멘트는 이 계정(me)이 단 것이고 첫 줄의 표시에 그 코멘트를
마지막으로 쓴 서버가 적혀 있다(같은 계정의 서버 둘이 같은 코멘트를 번갈아 고치는 경우).
"""

import json
from datetime import timedelta
from typing import Final

import pytest
from sqlmodel import Session, select

from app.shared.comment_pure import COMMENT_MARKER, read_owner, result_marker
from app.shared.db_pure import Run
from app.shared.job_pure import PRJob
from app.shared.ops_alert_pure import MARKER_DIR, other_server_text
from app.shared.other_server_pure import DEFER_WAIT, Owner, Server
from app.worker import feedback_job, graph, main
from app.worker.git import repo
from app.worker.git.github import GitHub
from app.worker.model.claude import claude_cli

from .helpers import HEAD, ME, ME_VERSION, PASSED, bodies, use_runner
from .test_feedback_job import FeedbackGh, _thread
from .test_feedback_job import _comment as _review_comment

RUN_FLOW: Final = graph.run_flow
OTHER: Final = "bbbb2222"
OLD_HEAD: Final = "c" * 40
COMMENT_ID: Final = 77


def _payload(kind: str = "update") -> str:
    return PRJob(
        repo="o/r", pr_number=1, kind=kind, head_sha=HEAD, base_sha="b" * 40, source="sync"
    ).model_dump_json()


def _owned_by(env, server: str, version: str, head: str) -> None:
    env["gh"].comments = f"{COMMENT_ID}\tme\t{result_marker(server, version, head)}\n"


def _states(env) -> list[str]:
    with Session(env["deps"].store.engine) as session:
        return [r.state for r in session.exec(select(Run).order_by(Run.id)).all()]


def _no_work(env) -> None:
    """체크아웃, 검사 흐름, 테스트, 모델 호출을 하면 실패한다."""
    patch = env["monkeypatch"].setattr
    fail = lambda *a, **k: pytest.fail("검사하지 않아야 한다")  # noqa: E731
    patch(repo, "checkout", fail)
    patch(repo, "ensure_bare", fail)
    patch(graph, "run_flow", fail)
    patch(graph, "carry_flow", fail)
    patch(claude_cli, "run_claude", fail)
    patch(claude_cli, "run_structured", fail)
    use_runner(env, fail)


def _age_runs(env, minutes: float) -> None:
    """기록의 시각을 그만큼 앞으로 옮긴다 (기다리는 시간이 지난 것으로)."""
    with Session(env["deps"].store.engine) as session:
        for run in session.exec(select(Run)).all():
            run.finished_at = run.finished_at - timedelta(minutes=minutes)
            run.heartbeat_at = run.finished_at
            session.add(run)
        session.commit()


def _last_marker(env) -> Owner | None:
    patched = [stdin for args, stdin in env["gh"].calls if stdin and "PATCH" in args]
    return read_owner(json.loads(patched[-1])["body"]) if patched else None


def test_single_server_comment_changes_only_in_the_marker_line(env, notifications) -> None:
    """서버 하나만 돌 때: 코멘트를 찾는 횟수도 고치는 차례도 같고 첫 줄만 이 서버의 표시다."""
    use_runner(env, lambda *a, **k: PASSED)
    assert main.handle(_payload(), env["deps"]) == "passed"
    assert env["gh"].kinds() == ["find", "create", "patch", "patch", "patch"]
    for body in bodies(env["gh"]):
        first, _, rest = body.partition("\n")
        assert first == result_marker(ME, ME_VERSION, HEAD) and COMMENT_MARKER not in rest
    assert _states(env) == ["done"] and notifications == []


def test_old_marker_without_server_is_checked_and_taken(env, notifications) -> None:
    env["gh"].comments = f"{COMMENT_ID}\tme\t{COMMENT_MARKER}\n"
    use_runner(env, lambda *a, **k: PASSED)
    assert main.handle(_payload(), env["deps"]) == "passed"
    assert env["gh"].kinds()[:2] == ["find", "patch"]  # 찾은 코멘트를 다시 찾지 않고 고친다
    assert _last_marker(env) == Owner(ME, ME_VERSION, HEAD)
    assert notifications == []


def test_same_version_already_did_this_commit(env, notifications) -> None:
    _owned_by(env, OTHER, ME_VERSION, HEAD)
    _no_work(env)
    assert main.handle(_payload(), env["deps"]) == "elsewhere"
    assert env["gh"].kinds() == ["find"]  # 코멘트를 고치지 않는다
    assert not any(stdin for _, stdin in env["gh"].calls)  # PR에 아무것도 달지 않는다
    assert _states(env) == ["elsewhere"]
    assert notifications == [other_server_text("o/r", ME_VERSION, ME_VERSION, 0)]


def test_higher_version_on_another_commit_is_deferred_then_taken_over(
    env, notifications, caplog
) -> None:
    fake_checkout = repo.checkout  # env가 넣은 가짜 체크아웃
    _owned_by(env, OTHER, "9.0.0", OLD_HEAD)
    _no_work(env)
    assert main.handle(_payload(), env["deps"]) == "deferred"
    assert env["gh"].kinds() == ["find"]
    assert notifications == [other_server_text("o/r", ME_VERSION, "9.0.0", 1)]
    # 기다리는 시간이 지나기 전에 다시 와도 미룬다
    assert main.handle(_payload(), env["deps"]) == "deferred"
    # 시간이 지나도 그 서버가 이 커밋을 처리하지 않았다: 꺼졌다고 보고 넘겨받는다
    _age_runs(env, DEFER_WAIT.total_seconds() / 60)
    _owned_by(env, OTHER, "9.0.0", OLD_HEAD)
    env["monkeypatch"].setattr(graph, "run_flow", RUN_FLOW)
    env["monkeypatch"].setattr(repo, "checkout", fake_checkout)
    use_runner(env, lambda *a, **k: PASSED)
    assert main.handle(_payload(), env["deps"]) == "passed"
    assert _last_marker(env) == Owner(ME, ME_VERSION, HEAD)
    assert _states(env) == ["deferred", "deferred", "done"]
    assert "넘겨받아 검사함" in caplog.text
    assert len(notifications) == 1  # 같은 저장소·서버 짝은 한 번만 알린다


def test_deferred_and_the_other_server_did_it_meanwhile(env) -> None:
    _owned_by(env, OTHER, ME_VERSION, OLD_HEAD)
    _no_work(env)
    assert main.handle(_payload(), env["deps"]) == "deferred"
    _age_runs(env, 2 * DEFER_WAIT.total_seconds() / 60)
    _owned_by(env, OTHER, ME_VERSION, HEAD)
    assert main.handle(_payload(), env["deps"]) == "elsewhere"
    assert env["gh"].kinds() == ["find", "find"]


def test_lower_version_is_taken_over_and_recommended(env, notifications) -> None:
    _owned_by(env, OTHER, "0.1.2", HEAD)
    use_runner(env, lambda *a, **k: PASSED)
    assert main.handle(_payload(), env["deps"]) == "passed"
    assert _last_marker(env) == Owner(ME, ME_VERSION, HEAD)
    assert notifications == [other_server_text("o/r", ME_VERSION, "0.1.2", -1)]
    assert (env["deps"].settings.data_dir / MARKER_DIR / f"other_server.o+r.{OTHER}").is_file()


def test_human_recheck_checks_whatever_the_marker(env, notifications) -> None:
    _owned_by(env, OTHER, "9.0.0", HEAD)
    use_runner(env, lambda *a, **k: PASSED)
    job = PRJob(repo="o/r", pr_number=1, kind="command", command="/recheck", comment_id=9,
                source="webhook")  # fmt: skip
    assert main.handle(job.model_dump_json(), env["deps"]) == "recheck:passed"
    assert _last_marker(env) == Owner(ME, ME_VERSION, HEAD)
    assert notifications == []


def test_feedback_is_left_to_the_server_that_owns_the_comment(env, notifications) -> None:
    gh = FeedbackGh()
    gh.threads = [_thread("T1", _review_comment(101))]
    gh.comments = f"{COMMENT_ID}\tme\t{result_marker(OTHER, ME_VERSION, HEAD)}\n"
    env["deps"].github = GitHub(gh, server=Server(ME, ME_VERSION))
    _no_work(env)
    job = PRJob(repo="o/r", pr_number=1, kind="feedback", source="webhook")
    assert feedback_job.process_feedback(job, env["deps"]) == "other_server"
    assert gh.replies == [] and gh.writes() == []
    assert env["deps"].store.handled_feedback("o/r", 1) == []
    # 판이 낮은 서버의 것이면 이 서버가 다룬다
    gh.comments = f"{COMMENT_ID}\tme\t{result_marker(OTHER, '0.0.1', HEAD)}\n"
    env["monkeypatch"].setattr(feedback_job, "handle_items", lambda *a: "handled")
    assert feedback_job.process_feedback(job, env["deps"]) == "handled"
