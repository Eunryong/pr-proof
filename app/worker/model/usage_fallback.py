"""claude의 사용량 한도에 닿으면 한도가 풀릴 때까지 로컬 모델로 도는 장치.

한도에 닿은 호출은 풀리는 시각을 데이터 폴더의 파일에 적고 환경 오류(`UsageLimitReached`)로
끝난다. RQ가 작업을 다시 넣으면 `active`가 그 파일을 보고 로컬 모델 구현을 고른다. 작업 하나는
처음부터 끝까지 한 모델로 돈다(저장한 답의 키에 모델 이름이 들어가므로 섞지 않는다).
"""

import logging
import os
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final, TypeVar

from app.shared.config import ModelConfig
from app.shared.model_names_pure import CLAUDE_CLI_BACKEND
from app.worker.model import usage_limit_pure
from app.worker.model.local.local_backend import LocalOpenAIBackend
from app.worker.model.model_backend import (
    AIEnvironmentError,
    AIErrorKind,
    AskModel,
    Capabilities,
    EditResult,
    ModelBackend,
    StartupCheck,
    StructuredResult,
)
from app.worker.model.usage_limit_pure import Limited

log = logging.getLogger(__name__)

_R = TypeVar("_R", StructuredResult, EditResult)

LIMIT_FILE: Final = "claude_usage_limit"  # 데이터 폴더의 파일. 내용은 로컬 모델로 도는 끝 시각


class UsageLimitReached(AIEnvironmentError):
    """claude가 사용량 한도에 닿았다. 작업을 다시 넣으면 로컬 모델로 돈다."""


def limited_until(data_dir: Path) -> float | None:
    """로컬 모델로 도는 끝 시각(초 단위 epoch). 파일이 없거나 읽지 못하면 None."""
    try:
        return float((data_dir / LIMIT_FILE).read_text().strip())
    except (OSError, ValueError):
        return None


def mark_limited(data_dir: Path, until: float) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    tmp = data_dir / f".{LIMIT_FILE}.{os.getpid()}.{secrets.token_hex(4)}"
    tmp.write_text(f"{until:.0f}\n")
    tmp.replace(data_dir / LIMIT_FILE)


def active(model: ModelConfig, data_dir: Path, primary: ModelBackend) -> tuple[ModelBackend, bool]:
    """이 작업이 쓸 모델 구현과, 로컬 모델로 넘긴 것인지.

    `model.fallback`이 없거나 고른 구현이 claude CLI가 아니면 primary 그대로다. 있으면 한도에
    닿아 있는 동안은 로컬 모델 구현, 아니면 한도에 닿는지 지켜보는 primary다.
    """
    if model.fallback is None or model.backend != CLAUDE_CLI_BACKEND:
        return primary, False
    until = limited_until(data_dir)
    if until is not None and time.time() < until:
        local = LocalOpenAIBackend(
            base_url=model.fallback.base_url, model_override=model.fallback.model
        )
        return local, True
    return LimitWatch(primary, data_dir), False


def check(result: _R, data_dir: Path, now: float) -> _R:
    """호출의 결과가 사용량 한도라면 로컬 모델로 도는 끝 시각을 적고 환경 오류로 올린다. 아니면
    결과를 그대로 돌려준다."""
    if result.error != AIErrorKind.USAGE_LIMIT:
        return result
    until = usage_limit_pure.until(Limited(result.limit_resets_at), now)
    mark_limited(data_dir, until)
    log.warning("claude 사용량 한도에 닿음. %s까지 로컬 모델로 돈다", time.ctime(until))
    raise UsageLimitReached(f"claude 사용량 한도에 닿음: {result.detail}")


class LimitWatch:
    """모델 구현을 감싸, 호출이 사용량 한도로 실패하면 풀리는 시각을 적고 환경 오류로 올린다."""

    def __init__(self, inner: ModelBackend, data_dir: Path) -> None:
        self._inner, self._data_dir = inner, data_dir

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def capabilities(self) -> Capabilities:
        return self._inner.capabilities

    def startup_check(self) -> StartupCheck:
        return self._inner.startup_check()

    def ask(self, prompt: str, **kwargs: object) -> StructuredResult:
        return self._checked(self._inner.ask(prompt, **kwargs))

    @contextmanager
    def reading(self, worktree: Path) -> Iterator[AskModel]:
        with self._inner.reading(worktree) as ask:

            def watched(prompt: str, **kwargs: object) -> StructuredResult:
                return self._checked(ask(prompt, **kwargs))

            yield watched

    def fix(self, worktree: Path, prompt: str, **kwargs: object) -> EditResult:
        return self._checked(self._inner.fix(worktree, prompt, **kwargs))

    def _checked(self, result: _R) -> _R:
        return check(result, self._data_dir, time.time())
