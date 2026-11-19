"""신뢰되지 않은 사람이 연 PR에는 아무것도 하지 않는다 (DESIGN 8.6의 임시 가드레일, 위협 표 T2).

워커가 GitHub 조회에서 읽은 작성자 값으로 정한다. 체크아웃·샌드박스·모델·코멘트·기록이 없다.
"""

from typing import Final

import pytest
from sqlmodel import Session, select

from app.models import state_keys
from app.models.db import Run
from app.models.job import PRJob
from app.worker import graph, main, repo
from app.worker.ai import claude_cli
from app.worker.github import GitHubError

from .helpers import HEAD, PASSED, use_runner

UNTRUSTED: Final = [
    "CONTRIBUTOR",
    "FIRST_TIME_CONTRIBUTOR",
    "FIRST_TIMER",
    "NONE",
    "SOMETHING_NEW",
    "",
]
NEW_HEAD: Final = "c" * 40


def _update(head: str = HEAD) -> str:
    return PRJob(
        repo="o/r", pr_number=1, kind="update", head_sha=head, base_sha="b" * 40, source="webhook"
    ).model_dump_json()


def _command(text: str) -> str:
    return PRJob(
        repo="o/r", pr_number=1, kind="command", command=text, comment_id=9, source="sync"
    ).model_dump_json()


def _forbid_everything(env) -> None:
    """체크아웃, 샌드박스의 테스트, 모델 호출이 불리면 실패한다."""
    patch = env["monkeypatch"].setattr
    patch(repo, "checkout", lambda *a, **k: pytest.fail("체크아웃하면 안 된다"))
    patch(graph, "run_pr_tests", lambda *a, **k: pytest.fail("테스트를 돌리면 안 된다"))
    patch(claude_cli, "run_claude", lambda *a, **k: pytest.fail("모델을 부르면 안 된다"))
    patch(claude_cli, "run_structured", lambda *a, **k: pytest.fail("모델을 부르면 안 된다"))


def _writes(gh) -> list[list[str]]:
    """PR 조회가 아닌 gh 호출."""
    return [args for args, stdin in gh.calls if stdin is not None or "-X" in args]


def _runs(env) -> list[Run]:
    with Session(env["deps"].store.engine) as session:
        return list(session.exec(select(Run)).all())


@pytest.mark.parametrize("association", UNTRUSTED)
def test_update_of_untrusted_author_does_nothing(env, association: str) -> None:
    env["gh"].author_association = association
    env["gh"].head = NEW_HEAD
    _forbid_everything(env)

    assert main.handle(_update(NEW_HEAD), env["deps"]) == "untrusted_author"

    assert len(env["gh"].calls) == 1 and _writes(env["gh"]) == []  # 조회 한 번뿐, 코멘트 없음
    assert _runs(env) == []
    # 최신 head도 건드리지 않는다
    assert env["redis"].get(state_keys.latest_head("o/r", 1)).decode() == HEAD


@pytest.mark.parametrize("text", ["/recheck", "/fix lint", "/fix static", "/mutate"])
def test_command_on_untrusted_authors_pr_does_nothing(env, text: str) -> None:
    """명령을 단 사람이 신뢰돼도 승인이 아니다. 승인은 정식 가드레일에서 넣는다."""
    env["gh"].author_association = "NONE"
    _forbid_everything(env)

    assert main.handle(_command(text), env["deps"]) == "untrusted_author"

    assert _writes(env["gh"]) == [] and _runs(env) == []
    assert env["deps"].store.command_handled(9)  # 동기화가 같은 명령을 다시 넣지 않는다


@pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_trusted_author_runs_as_before(env, association: str) -> None:
    env["gh"].author_association = association
    use_runner(env, lambda *a, **k: PASSED)
    assert main.handle(_update(), env["deps"]) == "passed"
    assert main.handle(_command("/recheck"), env["deps"]) == "recheck:passed"


def test_the_decision_uses_the_value_read_now_not_an_earlier_one(env) -> None:
    """큐에서 기다리는 사이에 권한이 빠지면 막힌다."""
    use_runner(env, lambda *a, **k: PASSED)
    assert main.handle(_update(), env["deps"]) == "passed"
    env["gh"].author_association = "NONE"
    env["gh"].head = NEW_HEAD
    _forbid_everything(env)
    assert main.handle(_update(NEW_HEAD), env["deps"]) == "untrusted_author"


def test_failed_lookup_runs_nothing(env) -> None:
    env["gh"].head_error = "HTTP 502"
    _forbid_everything(env)
    with pytest.raises(GitHubError):
        main.handle(_update(), env["deps"])
    assert _runs(env) == []
