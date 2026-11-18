"""worker/command.py: PR 코멘트 명령. Docker·GitHub 없이."""

import json

import pytest

from app.models.command import parse_command
from app.models.job import PRJob
from app.worker import command, repo, retry
from app.worker.github import GitHubError

from .helpers import PASSED, bodies, use_runner
from .test_github import is_reply


def _command(text: str, comment_id: int = 9) -> str:
    return PRJob(
        repo="o/r", pr_number=1, kind="command", command=text, comment_id=comment_id,
        source="webhook",
    ).model_dump_json()  # fmt: skip


def _replies(gh) -> list[str]:
    return [json.loads(stdin)["body"] for _, stdin in gh.calls if is_reply(stdin)]


def _base_yaml(env, text: str | None) -> None:
    files = {} if text is None else {".pipeline.yaml": text}
    env["monkeypatch"].setattr(repo, "read_base_files_from_cache", lambda *a, **k: files)


@pytest.mark.parametrize(
    ("text", "name", "arg"),
    [
        ("/recheck", "recheck", ""),
        ("/ReCheck", "recheck", ""),
        ("/fix lint", "fix", "lint"),
        ("/mutate", "extension", "mutation"),
        ("/generate-tests", "extension", "ai_tests"),
        ("/cc @someone", "unknown", ""),
    ],
)
def test_parse_command(text: str, name: str, arg: str) -> None:
    parsed = parse_command(text)
    assert (parsed.name, parsed.arg) == (name, arg)


def test_recheck_runs_current_head(env) -> None:
    from app.worker import main

    use_runner(env, lambda *a, **k: PASSED)
    assert main.handle(_command("/recheck"), env["deps"]) == "recheck:passed"
    assert "**테스트:** 통과 (9개)" in bodies(env["gh"])[-1]
    assert env["deps"].store.command_handled(9)


def test_same_comment_is_handled_once(env) -> None:
    from app.worker import main

    use_runner(env, lambda *a, **k: PASSED)
    main.handle(_command("/recheck"), env["deps"])
    calls = len(env["gh"].calls)
    assert main.handle(_command("/recheck"), env["deps"]) == "duplicate"
    assert len(env["gh"].calls) == calls


def test_recheck_on_closed_pr_does_nothing(env) -> None:
    from app.worker import main

    env["gh"].state = "closed"
    use_runner(env, lambda *a, **k: pytest.fail("닫힌 PR은 검사하지 않는다"))
    assert main.handle(_command("/recheck"), env["deps"]) == "closed"


@pytest.mark.parametrize(
    "yaml_text",
    [None, "extensions:\n  mutation:\n    enabled: false\n", "extensions: ["],
    ids=["no-config", "disabled", "broken-config"],
)
def test_disabled_extension_says_off(env, yaml_text) -> None:
    from app.worker import main

    _base_yaml(env, yaml_text)
    assert main.handle(_command("/mutate"), env["deps"]) == "extension_off"
    (body,) = _replies(env["gh"])
    assert "`/mutate`는 이 리포에서 꺼져 있습니다 (`extensions.mutation.enabled`)" in body
    # 같은 안내를 두 번 달지 않는다
    assert main.handle(_command("/mutate"), env["deps"]) == "duplicate"
    assert len(_replies(env["gh"])) == 1


def test_enabled_extension_is_not_supported_yet(env) -> None:
    from app.worker import main

    _base_yaml(env, "extensions:\n  ai_tests:\n    enabled: true\n")
    assert main.handle(_command("/generate-tests"), env["deps"]) == "extension_unsupported"
    assert "아직 이 파이프라인에서 지원하지 않습니다" in _replies(env["gh"])[0]


def test_unknown_command_gets_no_reply(env) -> None:
    from app.worker import main

    assert main.handle(_command("/cc @someone"), env["deps"]) == "unknown_command"
    assert env["gh"].calls == []


def test_fix_is_not_recorded_until_supported(env) -> None:
    from app.worker import main

    assert main.handle(_command("/fix lint"), env["deps"]) == "fix_not_ready"
    assert not env["deps"].store.command_handled(9)


@pytest.mark.parametrize("final", [False, True])
def test_github_failure_retries_and_releases_on_last(env, final: bool) -> None:
    from app.models import state_keys
    from app.worker import main

    env["gh"].head_error = "gh: 502"
    env["monkeypatch"].setattr(retry, "attempt", lambda: (4 if final else 1, final))
    job = PRJob.model_validate_json(_command("/mutate"))
    env["redis"].set(state_keys.job(job.dedup_key), "1")
    with pytest.raises(GitHubError):
        main.handle(_command("/mutate"), env["deps"])
    assert not env["deps"].store.command_handled(9)
    assert (env["redis"].get(state_keys.job(job.dedup_key)) is None) == final


def test_messages_quote_the_command() -> None:
    assert command.extension_off_message("/mutate", "mutation").startswith("> /mutate\n")
