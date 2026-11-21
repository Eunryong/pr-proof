"""worker/command.py: PR 코멘트 명령. Docker·GitHub 없이."""

import json

import pytest

from shared.job_pure import PRJob
from worker import command, retry
from worker.command_pure import parse_command
from worker.github import GitHubError

from .helpers import PASSED, bodies, use_runner
from .test_github import is_reply


def _command(text: str, comment_id: int = 9) -> str:
    return PRJob(
        repo="o/r", pr_number=1, kind="command", command=text, comment_id=comment_id,
        source="webhook",
    ).model_dump_json()  # fmt: skip


def _replies(gh) -> list[str]:
    return [json.loads(stdin)["body"] for _, stdin in gh.calls if is_reply(stdin)]


@pytest.mark.parametrize(
    ("text", "name", "arg"),
    [
        ("/recheck", "recheck", ""),
        ("/ReCheck", "recheck", ""),
        ("/recheck ABC1234", "recheck", "ABC1234"),
        ("/fix lint", "fix", "lint"),
        ("/fix Model", "fix", "model"),
        ("/fix", "fix", ""),
        ("/mutate", "unknown", ""),
        ("/generate-tests", "unknown", ""),
        ("/cc @someone", "unknown", ""),
    ],
)
def test_parse_command(text: str, name: str, arg: str) -> None:
    parsed = parse_command(text)
    assert (parsed.name, parsed.arg) == (name, arg)


def test_recheck_runs_current_head(env) -> None:
    from worker import main

    use_runner(env, lambda *a, **k: PASSED)
    assert main.handle(_command("/recheck"), env["deps"]) == "recheck:passed"
    assert "**테스트:** 통과 (9개)" in bodies(env["gh"])[-1]
    assert env["deps"].store.command_handled(9)


def test_same_comment_is_handled_once(env) -> None:
    from worker import main

    use_runner(env, lambda *a, **k: PASSED)
    main.handle(_command("/recheck"), env["deps"])
    calls = len(env["gh"].calls)
    assert main.handle(_command("/recheck"), env["deps"]) == "duplicate"
    assert len(env["gh"].calls) == calls


def test_recheck_on_closed_pr_does_nothing(env) -> None:
    from worker import main

    env["gh"].state = "closed"
    use_runner(env, lambda *a, **k: pytest.fail("닫힌 PR은 검사하지 않는다"))
    assert main.handle(_command("/recheck"), env["deps"]) == "closed"


@pytest.mark.parametrize("text", ["/cc @someone", "/mutate", "/generate-tests"])
def test_unknown_command_gets_no_reply(env, text: str) -> None:
    """확장 명령(v1 F13)은 범위에서 뺐다. 다른 모르는 명령처럼 답하지 않고 GitHub도 보지 않는다."""
    from worker import main

    assert main.handle(_command(text), env["deps"]) == "unknown_command"
    assert env["gh"].calls == []
    assert env["deps"].store.command_handled(9)  # 동기화가 같은 코멘트를 다시 넣지 않는다


@pytest.mark.parametrize("final", [False, True])
def test_github_failure_retries_and_releases_on_last(env, final: bool) -> None:
    from shared import state_keys_pure as state_keys
    from worker import main

    env["gh"].head_error = "gh: 502"
    env["monkeypatch"].setattr(retry, "attempt", lambda: (4 if final else 1, final))
    job = PRJob.model_validate_json(_command("/fix model"))
    env["redis"].set(state_keys.job(job.dedup_key), "1")
    with pytest.raises(GitHubError):
        main.handle(_command("/fix model"), env["deps"])
    # 마지막 시도면 실패로 적는다. 적지 않으면 동기화가 주기마다 다시 넣는다
    assert env["deps"].store.command_handled(9) == final
    assert (env["redis"].get(state_keys.job(job.dedup_key)) is None) == final


def test_messages_quote_the_command() -> None:
    assert command.fix_unsupported_message("/fix", "structure").startswith("> /fix structure\n")
    message = command.no_pending_fix_message("a" * 40, skill="lint", typed="mechanical")
    assert message.startswith("> /fix mechanical\n")
    assert command.no_pending_fix_message("a" * 40, skill="").startswith("> /fix\n")
