"""이름과 설정으로 모델 구현을 고른다 (docs/DESIGN.md 8.4 "인터페이스와 구현").

워커는 작업을 시작할 때(`app/worker/deps.py`, `app/worker/main.py`) 파이프라인 설정(`config.yaml`의
`model.backend`, `model.base_url`)으로 구현 하나를 만들어 호출하는 쪽에 넘긴다.
"""

from collections.abc import Callable, Mapping
from typing import Final

from app.shared.config import ModelConfig
from app.worker.model.claude.claude_backend import NAME as CLAUDE_CLI
from app.worker.model.claude.claude_backend import ClaudeCliBackend
from app.worker.model.local.local_backend import NAME as LOCAL_OPENAI
from app.worker.model.local.local_backend import LocalOpenAIBackend
from app.worker.model.model_backend import ModelBackend

# 이름 → 기본 설정으로 구현을 만드는 함수
BACKENDS: Final[Mapping[str, Callable[[], ModelBackend]]] = {
    CLAUDE_CLI: ClaudeCliBackend,
    LOCAL_OPENAI: LocalOpenAIBackend,
}
DEFAULT_BACKEND: Final = CLAUDE_CLI


class UnknownBackend(ValueError):
    """모르는 모델 구현의 이름."""


def make_backend(name: str) -> ModelBackend:
    """이름의 구현을 기본 설정으로 만든다. 모르는 이름이면 아는 이름들과 함께 UnknownBackend."""
    factory = BACKENDS.get(name)
    if factory is None:
        known = ", ".join(sorted(BACKENDS))
        raise UnknownBackend(f"모르는 모델 구현 {name!r}: 쓸 수 있는 것은 {known}")
    return factory()


def from_config(model: ModelConfig) -> ModelBackend:
    """파이프라인 설정의 `model` 묶음으로 구현을 만든다."""
    if model.backend == CLAUDE_CLI:
        return ClaudeCliBackend()
    if model.backend == LOCAL_OPENAI:
        return LocalOpenAIBackend(base_url=model.base_url)
    known = ", ".join(sorted(BACKENDS))
    raise UnknownBackend(f"모르는 모델 구현 {model.backend!r}: 쓸 수 있는 것은 {known}")


def default_backend() -> ModelBackend:
    """기본 모델 구현 (`DEFAULT_BACKEND`)."""
    return make_backend(DEFAULT_BACKEND)
