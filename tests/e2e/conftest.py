"""E2E 공용: 예제 리포의 README에는 규칙이 있으므로 문서 규칙 경로가 모델을 부른다. 실제 claude를
부르지 않도록 기본으로 막는다(도구 없는 호출과 파일 도구 호출 둘 다). 케이스 파일은 가짜 모델로
바꾸고, `PR_PROOF_REAL_MODEL=1`이면 막지 않는다. 그때 `PR_PROOF_MODEL_MEMO`가 있으면 호출을 그
파일에 남기고 같은 입력의 도구 없는 호출은 저장한 답을 다시 쓴다(`model_memo.py`)."""

import os
from pathlib import Path

import pytest

from app.worker.evaluation import MODEL_MEMO_ENV
from app.worker.model.claude import claude_cli
from app.worker.model.claude.claude_cli import AIErrorKind, ClaudeResult, StructuredResult

from .model_memo import Memo


def _no_real_model(*args, **kwargs) -> StructuredResult:
    return StructuredResult(
        ok=False, error=AIErrorKind.LAUNCH, data=None, model_id="", input_tokens=None,
        output_tokens=None, cost_usd=None, permission_denials=0,
        detail="E2E에서 실제 모델을 부르지 않음",
    )  # fmt: skip


def _no_real_fix(*args, **kwargs) -> ClaudeResult:
    return ClaudeResult(
        ok=False, error=AIErrorKind.EXIT, cost_usd=None, num_turns=0, permission_denials=(),
        text="", detail="E2E에서 실제 모델을 부르지 않음",
    )  # fmt: skip


def block_model(monkeypatch) -> None:
    monkeypatch.setattr(claude_cli, "run_structured", _no_real_model)
    monkeypatch.setattr(claude_cli, "run_claude", _no_real_fix)


@pytest.fixture(autouse=True)
def no_real_model(monkeypatch, request) -> None:
    if os.environ.get("PR_PROOF_REAL_MODEL") != "1":
        block_model(monkeypatch)
        return
    if memo_path := os.environ.get(MODEL_MEMO_ENV):
        memo = Memo(Path(memo_path))
        test = lambda: request.node.nodeid  # noqa: E731
        monkeypatch.setattr(claude_cli, "run_structured",
                            memo.structured(claude_cli.run_structured, test))  # fmt: skip
        monkeypatch.setattr(claude_cli, "run_claude", memo.fix(claude_cli.run_claude, test))


@pytest.fixture
def no_model(monkeypatch) -> None:
    """실제 모델로 도는 평가에서도 모델을 부르지 않는다(도구 설정의 규칙만 보는 lint 케이스)."""
    block_model(monkeypatch)
