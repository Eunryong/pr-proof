import os
from pathlib import Path
from typing import Final

import pytest

from app.shared import config, github_auth
from app.shared.ops_alert_pure import TEXT

TOKEN: Final = "github_pat_FAKE0000000000000000000000"


def _token_file(directory: Path, mode: int = 0o600, value: str = TOKEN) -> Path:
    path = directory / github_auth.TOKEN_FILE
    path.write_text(value + "\n")
    path.chmod(mode)
    return path


def test_without_token_file_gh_login_is_used(tmp_path: Path) -> None:
    env = github_auth.gh_env(["PATH", "HOME"], {"PATH": "/bin", "OTHER": "x"}, tmp_path)
    assert env == {"PATH": "/bin"}
    assert not github_auth.uses_token(tmp_path)
    assert github_auth.status_args(tmp_path) == ["auth", "status"]
    assert github_auth.alert_kind(tmp_path) == "gh_login"
    assert github_auth.hint(tmp_path) == "gh auth login"


def test_token_file_goes_into_gh_env_only(tmp_path: Path, monkeypatch) -> None:
    _token_file(tmp_path)
    monkeypatch.delenv(github_auth.TOKEN_ENV, raising=False)
    env = github_auth.gh_env(["PATH"], {"PATH": "/bin"}, tmp_path)
    assert env == {"PATH": "/bin", "GH_TOKEN": TOKEN}
    # 이 프로세스의 환경에는 넣지 않는다 (다른 하위 프로세스가 물려받지 않게)
    assert github_auth.TOKEN_ENV not in os.environ
    assert github_auth.status_args(tmp_path) == ["auth", "status", "--active"]
    assert github_auth.alert_kind(tmp_path) == "gh_token"
    assert "github_token" in github_auth.hint(tmp_path)


def test_ambient_gh_token_is_not_passed_without_file(tmp_path: Path) -> None:
    """서버 환경에 GH_TOKEN이 있어도 허용 목록에 없으므로 넘기지 않는다(파일만 토큰 방식을 켠다)."""
    env = github_auth.gh_env(["PATH"], {"PATH": "/bin", "GH_TOKEN": "ambient"}, tmp_path)
    assert env == {"PATH": "/bin"}


@pytest.mark.parametrize(("mode", "content"), [(0o644, TOKEN), (0o600, "")])
def test_bad_token_file_fails_loudly(tmp_path: Path, mode: int, content: str) -> None:
    _token_file(tmp_path, mode, content)
    with pytest.raises(config.ConfigError):
        github_auth.gh_env(["PATH"], {}, tmp_path)


def test_redact_removes_whole_token_before_cutting() -> None:
    env = {"GH_TOKEN": TOKEN}
    text = f"remote: invalid credentials for {TOKEN} at github.com"
    assert TOKEN not in github_auth.redact(text, env)
    assert github_auth.REDACTED in github_auth.redact(text, env)
    assert github_auth.redact(text, {}) == text


def test_token_alert_says_to_replace_token() -> None:
    assert "github_token" in TEXT["gh_token"] and "만료" in TEXT["gh_token"]


def test_failure_detail_never_comes_out_empty() -> None:
    """gh가 stderr 없이 실패해도 로그의 까닭이 비지 않는다. 토큰은 지운다."""
    env = {github_auth.TOKEN_ENV: "tok-123"}
    assert github_auth.failure_detail("", "", 1, env) == "종료 코드 1, 출력 없음"
    assert github_auth.failure_detail("", '{"message":"Not Found"} tok-123', 1, env) == (
        '{"message":"Not Found"} <github_token> (종료 코드 1)'
    )
    assert github_auth.failure_detail("HTTP 502\n", "ignored", 1, {}) == "HTTP 502 (종료 코드 1)"
