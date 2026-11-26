"""로컬 모델 구현의 부품: 주소 확인, 구현 고르기, 시간 상한, 서버가 없을 때의 실패.

모델이 답하는 흐름(ask, reading, fix)은 `tests/worker/model/test_model_backend_contract.py`가 실제
로컬 서버로 확인한다. 요청을 만들고 응답을 읽는 계산은 `test_openai_pure.py`에 있다.
"""

import socket
import subprocess
from pathlib import Path

import pytest

from app.shared.config import ModelConfig
from app.worker.git import repo
from app.worker.model import backends
from app.worker.model.local import local_backend
from app.worker.model.local.local_backend import LocalOpenAIBackend
from app.worker.model.model_backend import AIErrorKind


def _closed_port_url() -> str:
    """아무도 듣지 않는 이 컴퓨터의 포트. 잠깐 잡았다 놓아 번호만 얻는다."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}/v1"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    )  # fmt: skip


def test_non_local_base_url_is_rejected() -> None:
    with pytest.raises(ValueError, match="로컬 모델 서버 주소가 아님"):
        LocalOpenAIBackend(base_url="https://api.openai.com/v1")


def test_backends_from_config_selects_local_openai() -> None:
    backend = backends.from_config(
        ModelConfig(backend="local-openai", base_url="http://127.0.0.1:11434/v1")
    )
    assert isinstance(backend, LocalOpenAIBackend)
    assert backend.base_url == "http://127.0.0.1:11434/v1"


def test_timeout_defaults_to_and_is_capped_at_20_minutes() -> None:
    assert local_backend._effective_timeout(None) == 1200.0
    assert local_backend._effective_timeout(1800) == 1200.0
    assert local_backend._effective_timeout(30) == 30


def test_a_server_that_is_not_running_is_an_environment_failure(tmp_path: Path) -> None:
    backend = LocalOpenAIBackend(base_url=_closed_port_url())
    assert backend.startup_check().ok is False
    asked = backend.ask(
        "q", system_prompt="s", schema={"type": "object"}, model="m", max_budget_usd=0.1
    )
    assert not asked.ok and asked.error == AIErrorKind.LAUNCH

    wt = tmp_path / "wt"
    wt.mkdir()
    _git(wt, "init", "-q", "-b", "main")
    repo.record_git_dir(wt)
    (wt / "app.py").write_text("X = 1\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "init")
    fixed = backend.fix(wt, "고쳐라", system_prompt="지시", model="m", max_budget_usd=0.5)
    assert not fixed.ok and fixed.error == AIErrorKind.LAUNCH and fixed.environment
    assert (
        subprocess.run(
            ["git", "status", "--porcelain"], cwd=wt, capture_output=True, text=True
        ).stdout
        == ""
    )
