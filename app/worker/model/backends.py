"""이름으로 모델 구현을 고른다 (docs/DESIGN.md 8.4 "인터페이스와 구현").

워커는 작업을 시작할 때(`app/worker/deps.py`) 여기서 구현 하나를 만들어 호출하는 쪽에 넘긴다.
고를 설정은 아직 없어 늘 `DEFAULT_BACKEND`다. 설정에서 이름을 읽게 되면 그 이름을 `make_backend`에
넘긴다.
"""

from collections.abc import Callable, Mapping
from typing import Final

from app.worker.model.claude.claude_backend import NAME as CLAUDE_CLI
from app.worker.model.claude.claude_backend import ClaudeCliBackend
from app.worker.model.model_backend import ModelBackend

# 이름 → 구현을 만드는 함수
BACKENDS: Final[Mapping[str, Callable[[], ModelBackend]]] = {CLAUDE_CLI: ClaudeCliBackend}
DEFAULT_BACKEND: Final = CLAUDE_CLI


class UnknownBackend(ValueError):
    """모르는 모델 구현의 이름."""


def make_backend(name: str) -> ModelBackend:
    """이름의 구현을 만든다. 모르는 이름이면 아는 이름들과 함께 UnknownBackend."""
    factory = BACKENDS.get(name)
    if factory is None:
        known = ", ".join(sorted(BACKENDS))
        raise UnknownBackend(f"모르는 모델 구현 {name!r}: 쓸 수 있는 것은 {known}")
    return factory()


def default_backend() -> ModelBackend:
    """워커가 쓰는 구현 (설정이 없어 `DEFAULT_BACKEND`)."""
    return make_backend(DEFAULT_BACKEND)
