"""configure의 gh runner: 확인하려는 토큰은 gh에만 넘기고, 오류 글에서 지운다.

gh는 PATH 앞의 가짜다."""

from pathlib import Path
from typing import Final

import pytest

from app.setup import github_setup as gh

TOKEN: Final = "github_pat_FAKE3333333333333333333333"


@pytest.fixture
def stub(tmp_path: Path, monkeypatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "gh"
    script.write_text(
        f'#!/bin/sh\n/usr/bin/env > "{tmp_path}/gh.env"\n'
        f'if [ -f "{tmp_path}/DENY" ]; then echo "HTTP 403: no ($GH_TOKEN)" >&2; exit 1; fi\n'
        'echo "true\ttrue"\n'
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    return tmp_path


def _env(stub: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in (stub / "gh.env").read_text().splitlines()
                if "=" in line)  # fmt: skip


def test_candidate_token_goes_to_gh(stub) -> None:
    assert gh.repo_access(gh.make_runner(TOKEN), "octocat/hello-world")[:2] == (True, True)
    assert _env(stub)["GH_TOKEN"] == TOKEN


def test_saved_login_has_no_token(stub) -> None:
    gh.make_runner(None)(["auth", "status"], None)
    assert "GH_TOKEN" not in _env(stub)


def test_denied_is_no_permission_and_hides_token(stub) -> None:
    (stub / "DENY").write_text("")
    with pytest.raises(gh.NoPermission) as caught:
        gh.list_hooks(gh.make_runner(TOKEN), "octocat/hello-world")
    assert TOKEN not in str(caught.value)


def test_token_comes_from_env_not_from_arguments() -> None:
    assert gh.token_from_env({gh.TOKEN_INPUT_ENV: f"  {TOKEN}\n"}) == TOKEN
    assert gh.token_from_env({}) is None
