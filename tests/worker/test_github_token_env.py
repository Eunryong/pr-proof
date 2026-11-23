"""토큰 방식: 토큰은 gh와 GitHub과 주고받는 git에만 가고 모델·샌드박스·로그에는 가지 않는다.

gh와 git은 PATH 앞의 가짜로 바꿔 받은 환경을 파일에 적게 한다. 실제 GitHub을 부르지 않는다.
"""

import os
from pathlib import Path
from typing import Final

import pytest

from app.api import github as api_github
from app.shared import config, github_auth
from app.worker import github, preflight, repo
from app.worker.ai.claude_cli import claude_env
from app.worker.fix import proposal
from app.worker.sandbox.docker_cli import minimal_env

TOKEN: Final = "github_pat_FAKE1111111111111111111111"


@pytest.fixture
def token_dir(tmp_path: Path, monkeypatch) -> Path:
    directory = tmp_path / "config"
    directory.mkdir()
    path = directory / github_auth.TOKEN_FILE
    path.write_text(TOKEN)
    path.chmod(0o600)
    monkeypatch.setenv(config.CONFIG_DIR_ENV, str(directory))
    return directory


@pytest.fixture
def stubs(tmp_path: Path, monkeypatch) -> Path:
    """gh와 git 가짜. 받은 환경을 <이름>.env에 적는다. FAIL 파일이 있으면 GH_TOKEN을 stderr에
    찍고 실패한다."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("gh", "git"):
        script = bin_dir / name
        script.write_text(
            f'#!/bin/sh\n/usr/bin/env > "{tmp_path}/{name}.env"\n'
            f'if [ -f "{tmp_path}/FAIL" ]; then echo "bad credentials: $GH_TOKEN" >&2; exit 1; fi\n'
            "echo ok\n"
        )
        script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    return tmp_path


def _env_of(stubs: Path, name: str) -> dict[str, str]:
    lines = (stubs / f"{name}.env").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


@pytest.mark.parametrize("run", [lambda: github.run_gh(["api", "user"]),
                                 lambda: api_github.run_gh(["api", "user"])])  # fmt: skip
def test_gh_gets_token_from_file(token_dir, stubs, run) -> None:
    run()
    assert _env_of(stubs, "gh")["GH_TOKEN"] == TOKEN
    assert "GH_TOKEN" not in os.environ


def test_gh_without_token_file_uses_stored_login(stubs) -> None:
    github.run_gh(["api", "user"])
    assert "GH_TOKEN" not in _env_of(stubs, "gh")


@pytest.mark.parametrize(
    ("run", "error"),
    [(lambda: github.run_gh(["api", "user"]), github.GitHubError),
     (lambda: api_github.run_gh(["api", "user"]), api_github.GitHubReadError)],
)  # fmt: skip
def test_gh_failure_message_does_not_carry_token(token_dir, stubs, run, error) -> None:
    (stubs / "FAIL").write_text("")
    with pytest.raises(error) as caught:
        run()
    assert TOKEN not in str(caught.value)
    assert github_auth.REDACTED in str(caught.value)


def test_network_git_gets_token_and_local_git_does_not(token_dir, stubs) -> None:
    repo._git("fetch", "origin", network=True)
    assert _env_of(stubs, "git")["GH_TOKEN"] == TOKEN
    repo._git("status")
    assert "GH_TOKEN" not in _env_of(stubs, "git")


def test_git_failure_message_does_not_carry_token(token_dir, stubs) -> None:
    (stubs / "FAIL").write_text("")
    with pytest.raises(repo.GitError) as caught:
        repo._git("push", "origin", network=True)
    assert TOKEN not in str(caught.value)


def test_wide_token_file_is_an_error_not_a_silent_fallback(token_dir, stubs) -> None:
    (token_dir / github_auth.TOKEN_FILE).chmod(0o644)
    with pytest.raises(github.GitHubError, match="chmod 600"):
        github.run_gh(["api", "user"])


def test_model_and_sandbox_envs_never_get_token(token_dir, monkeypatch) -> None:
    """모델 호출(claude)과 샌드박스(docker)는 각자 허용 목록으로 환경을 고른다. 토큰 파일이 있어도,
    누가 서버 환경에 GH_TOKEN을 두었어도 넘기지 않는다."""
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    assert "GH_TOKEN" not in claude_env()
    assert "GH_TOKEN" not in minimal_env()
    assert TOKEN not in claude_env().values() and TOKEN not in minimal_env().values()


def test_token_is_a_pipeline_secret_for_the_diff_check(token_dir) -> None:
    assert TOKEN in proposal.pipeline_secrets()


def test_preflight_in_token_mode_says_to_replace_token(token_dir, tmp_path) -> None:
    from ..redis_stand_ins import Pong

    checks = preflight.run_checks(
        Pong(), command_ok=lambda args: True,
        claude_status=lambda: '{"loggedIn": true, "loginExpiresAt": null}', gh_ok=lambda: False,
    )  # fmt: skip
    gh = checks[0]
    assert (gh.name, gh.ok, gh.alert) == ("GitHub 인증", False, "gh_token")
    assert "github_token" in gh.hint
