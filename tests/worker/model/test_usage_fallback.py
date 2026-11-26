"""사용량 한도에 닿으면 풀릴 때까지 로컬 모델로 넘어간다.

모델을 부르지 않는 부분만 본다: 한도에 닿은 결과를 받았을 때 하는 일(`check`)과, 적어 둔 시각에
따라 구현을 고르는 일(`active`).
"""

import time
from pathlib import Path

import pytest

from app.shared.config import LocalFallback, ModelConfig
from app.worker.model import usage_fallback
from app.worker.model.claude.claude_backend import ClaudeCliBackend
from app.worker.model.local.local_backend import LocalOpenAIBackend
from app.worker.model.model_backend import AIErrorKind, StructuredResult
from app.worker.model.usage_fallback import (
    LimitWatch,
    UsageLimitReached,
    active,
    check,
    limited_until,
)
from app.worker.model.usage_limit_pure import REPROBE_SECONDS

MODEL = ModelConfig(fallback=LocalFallback(model="inkling-2-mini"))
NOW = 1_000_000.0


def _result(error: AIErrorKind | None, resets: float | None = None) -> StructuredResult:
    return StructuredResult(
        ok=error is None, error=error, data={} if error is None else None, model_id="m",
        input_tokens=1, output_tokens=1, cost_usd=0.0, permission_denials=0,
        detail="d", limit_resets_at=resets,
    )  # fmt: skip


def test_a_limited_result_marks_the_reset_time_and_raises_an_environment_error(
    tmp_path: Path,
) -> None:
    with pytest.raises(UsageLimitReached):
        check(_result(AIErrorKind.USAGE_LIMIT, NOW + 3_600), tmp_path, NOW)
    assert limited_until(tmp_path) == NOW + 3_600


def test_without_a_reset_time_the_mark_is_the_reprobe_time(tmp_path: Path) -> None:
    with pytest.raises(UsageLimitReached):
        check(_result(AIErrorKind.USAGE_LIMIT), tmp_path, NOW)
    assert limited_until(tmp_path) == NOW + REPROBE_SECONDS


def test_other_results_pass_through_without_a_mark(tmp_path: Path) -> None:
    for result in (_result(None), _result(AIErrorKind.EXIT), _result(AIErrorKind.TOOLS)):
        assert check(result, tmp_path, NOW) is result
    assert limited_until(tmp_path) is None


def test_the_backend_follows_the_mark(tmp_path: Path) -> None:
    primary = ClaudeCliBackend()
    assert active(ModelConfig(), tmp_path, primary) == (primary, False)  # fallback이 없다
    backend, fallen = active(MODEL, tmp_path, primary)
    assert isinstance(backend, LimitWatch) and not fallen  # 한도에 닿은 적이 없다
    usage_fallback.mark_limited(tmp_path, time.time() + 60)
    backend, fallen = active(MODEL, tmp_path, primary)
    assert isinstance(backend, LocalOpenAIBackend) and fallen
    usage_fallback.mark_limited(tmp_path, time.time() - 1)
    backend, fallen = active(MODEL, tmp_path, primary)
    assert isinstance(backend, LimitWatch) and not fallen  # 풀리는 시각이 지났다
