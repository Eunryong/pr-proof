"""E2E 공용: 예제 리포의 README에는 규칙이 있으므로 문서 규칙 경로가 모델을 부른다. 실제 claude를
부르지 않도록 기본으로 막는다. 문서 규칙 케이스(test_rules_cases.py)는 가짜 모델로 바꾸고,
`PR_PROOF_REAL_MODEL=1`이면 막지 않는다."""

import os

import pytest

from app.worker.ai import claude_cli
from app.worker.ai.claude_cli import AIErrorKind, StructuredResult


def _no_real_model(*args, **kwargs) -> StructuredResult:
    return StructuredResult(
        ok=False, error=AIErrorKind.LAUNCH, data=None, model_id="", input_tokens=None,
        output_tokens=None, cost_usd=None, permission_denials=0,
        detail="E2E에서 실제 모델을 부르지 않음",
    )  # fmt: skip


@pytest.fixture(autouse=True)
def no_real_model(monkeypatch) -> None:
    if os.environ.get("PR_PROOF_REAL_MODEL") != "1":
        monkeypatch.setattr(claude_cli, "run_structured", _no_real_model)
