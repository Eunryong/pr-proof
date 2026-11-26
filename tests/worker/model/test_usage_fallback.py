"""사용량 한도에 닿으면 풀릴 때까지 로컬 모델로 넘어간다."""

import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from app.shared.config import LocalFallback, ModelConfig
from app.worker.model import usage_fallback
from app.worker.model.local.local_backend import LocalOpenAIBackend
from app.worker.model.model_backend import AIErrorKind, StructuredResult
from app.worker.model.usage_fallback import LimitWatch, UsageLimitReached, active, limited_until


def _result(error: AIErrorKind | None, resets: float | None = None) -> StructuredResult:
    return StructuredResult(
        ok=error is None, error=error, data={} if error is None else None, model_id="m",
        input_tokens=1, output_tokens=1, cost_usd=0.0, permission_denials=0,
        detail="d", limit_resets_at=resets,
    )  # fmt: skip


class _Primary:
    name = "claude-cli"
    capabilities = None

    def __init__(self, results: list[StructuredResult]) -> None:
        self.results = results

    def ask(self, prompt: str, **kwargs: object) -> StructuredResult:
        return self.results.pop(0)

    @contextmanager
    def reading(self, worktree: Path):
        yield self.ask


MODEL = ModelConfig(fallback=LocalFallback(model="inkling-2-mini"))


def test_without_a_fallback_the_backend_is_untouched(tmp_path: Path) -> None:
    primary = _Primary([])
    assert active(ModelConfig(), tmp_path, primary) == (primary, False)  # type: ignore[arg-type]


def test_a_limited_call_marks_the_time_and_raises_an_environment_error(tmp_path: Path) -> None:
    resets = time.time() + 3_600
    primary = _Primary([_result(None), _result(AIErrorKind.USAGE_LIMIT, resets)])
    backend, fallen = active(MODEL, tmp_path, primary)  # type: ignore[arg-type]
    assert isinstance(backend, LimitWatch) and not fallen
    assert backend.ask("p").ok and limited_until(tmp_path) is None
    with pytest.raises(UsageLimitReached):
        backend.ask("p")
    assert limited_until(tmp_path) == pytest.approx(resets, abs=1)


def test_reading_calls_are_watched_too(tmp_path: Path) -> None:
    primary = _Primary([_result(AIErrorKind.USAGE_LIMIT)])
    backend, _ = active(MODEL, tmp_path, primary)  # type: ignore[arg-type]
    with backend.reading(tmp_path) as ask, pytest.raises(UsageLimitReached):
        ask("p")
    left = limited_until(tmp_path)
    assert left is not None and left > time.time()  # 풀리는 시각을 모르면 다시 불러 볼 시각


def test_while_limited_the_job_runs_on_the_local_model_then_returns(tmp_path: Path) -> None:
    usage_fallback.mark_limited(tmp_path, time.time() + 60)
    backend, fallen = active(MODEL, tmp_path, _Primary([]))  # type: ignore[arg-type]
    assert isinstance(backend, LocalOpenAIBackend) and fallen
    usage_fallback.mark_limited(tmp_path, time.time() - 1)
    backend, fallen = active(MODEL, tmp_path, _Primary([]))  # type: ignore[arg-type]
    assert isinstance(backend, LimitWatch) and not fallen


def test_other_failures_pass_through(tmp_path: Path) -> None:
    primary = _Primary([_result(AIErrorKind.EXIT)])
    backend, _ = active(MODEL, tmp_path, primary)  # type: ignore[arg-type]
    assert backend.ask("p").error == AIErrorKind.EXIT and limited_until(tmp_path) is None
