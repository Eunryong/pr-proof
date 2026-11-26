"""E2E 공용. 모델이 필요한 테스트는 `PR_PROOF_REAL_MODEL=1`일 때만 돈다(`needs_model`). 그 값이
없으면 모델을 부르는 순간 테스트가 실패한다. `PR_PROOF_BACKEND=local-openai`면 로컬 모델 서버를
쓰고(`PR_PROOF_LOCAL_BASE_URL`, `PR_PROOF_LOCAL_MODEL`), `PR_PROOF_MODEL_MEMO`가 있으면 호출을 그
파일에 남기고 같은 입력의 도구 없는 호출은 저장한 답을 다시 쓴다(`model_memo.py`)."""

import os
from pathlib import Path

import pytest

from app.shared.model_names_pure import DEFAULT_LOCAL_OPENAI_URL, LOCAL_OPENAI_BACKEND
from app.worker.evaluation.evaluation import MODEL_MEMO_ENV, REAL_MODEL_ENV
from app.worker.model import backends
from app.worker.model.claude import claude_cli
from app.worker.model.local import openai_client
from app.worker.model.local.local_backend import LocalOpenAIBackend

from .model_memo import Memo


def model_allowed() -> bool:
    return os.environ.get(REAL_MODEL_ENV) == "1"


def needs_model() -> None:
    if not model_allowed():
        pytest.skip(f"모델을 부르는 테스트 ({REAL_MODEL_ENV}=1)")


def _refuse(*args, **kwargs):
    pytest.fail(f"{REAL_MODEL_ENV}=1 없이 모델을 불렀다")


@pytest.fixture(autouse=True)
def model_calls(monkeypatch, request) -> None:
    if not model_allowed():
        monkeypatch.setattr(claude_cli, "run_structured", _refuse)
        monkeypatch.setattr(claude_cli, "run_claude", _refuse)
        monkeypatch.setattr(openai_client, "post_chat", _refuse)
        return
    if os.environ.get("PR_PROOF_BACKEND") == LOCAL_OPENAI_BACKEND:
        base_url = os.environ.get("PR_PROOF_LOCAL_BASE_URL", DEFAULT_LOCAL_OPENAI_URL)
        model_override = os.environ.get("PR_PROOF_LOCAL_MODEL") or None
        monkeypatch.setattr(
            backends,
            "default_backend",
            lambda: LocalOpenAIBackend(base_url=base_url, model_override=model_override),
        )
        return
    if memo_path := os.environ.get(MODEL_MEMO_ENV):
        memo = Memo(Path(memo_path))
        test = lambda: request.node.nodeid  # noqa: E731
        monkeypatch.setattr(claude_cli, "run_structured",
                            memo.structured(claude_cli.run_structured, test))  # fmt: skip
        monkeypatch.setattr(claude_cli, "run_claude", memo.fix(claude_cli.run_claude, test))
