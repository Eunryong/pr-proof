"""파이프라인 쪽 설정과 비밀값 읽기.

설정: `<설정 폴더>/config.yaml`. 비밀값: `<설정 폴더>/<이름>` 파일 (chmod 600).
설정 폴더는 기본 `~/.config/pipeline`이고, `PIPELINE_CONFIG_DIR`로 경로만 바꿀 수 있다.
비밀값 자체는 환경변수로 받지 않는다.
"""

import os
import stat
from pathlib import Path
from typing import Final

import yaml
from pydantic import BaseModel, field_validator

from app.models.rag import E5_SMALL, EMBED_MODELS

DEFAULT_CONFIG_DIR: Final = Path.home() / ".config" / "pipeline"
CONFIG_DIR_ENV: Final = "PIPELINE_CONFIG_DIR"
WEBHOOK_SECRET: Final = "webhook_secret"


class ConfigError(RuntimeError):
    """설정이나 비밀값 파일이 없거나 잘못됐다."""


class SyncConfig(BaseModel):
    interval_minutes: float = 5  # 0이면 동기화 루프를 띄우지 않는다
    env_error_backoff_minutes: float = 30
    # .venv 설치(최대 15분) 동안은 생존 확인을 남기지 못하므로 그보다 길게 둔다
    heartbeat_timeout_minutes: float = 20


class SandboxConfig(BaseModel):
    # 대상 리포가 .pipeline.yaml의 sandbox.memory_gb로 올릴 수 있는 상한
    max_memory_gb: float = 4
    # 이미지 태그와 작업 컨테이너 이름 접두어. 기본값은 app/sandbox/docker_cli.py와 같다
    # (tests/test_config.py가 확인). 같은 Docker를 쓰는 다른 실행과 겹치지 않게 바꿀 수 있다
    image: str = "pipeline-sandbox"
    container_prefix: str = "pr-proof-job"


class CallMaxima(BaseModel):
    """리포의 `.pipeline.yaml`이 `model.calls`로 정할 수 있는 각 값의 최댓값 (DESIGN.md 10절)."""

    max_pr_max: int = 500
    max_ruleset_max: int = 300
    max_repo_daily: int = 2000


class ModelConfig(BaseModel):
    calls: CallMaxima = CallMaxima()


class RagConfig(BaseModel):
    """과거 기록 검색의 파이프라인 쪽 설정 (DESIGN.md 6.5, 10절). 모델은 `models/rag.py`의
    `EMBED_MODELS`에 판과 해시를 고정한 것만 받는다."""

    embed_model: str = E5_SMALL

    @field_validator("embed_model")
    @classmethod
    def _known(cls, value: str) -> str:
        if value not in EMBED_MODELS:
            known = ", ".join(sorted(EMBED_MODELS))
            raise ValueError(f"받을 수 있는 임베딩 모델이 아님: {value} (가능: {known})")
        return value


class PipelineConfig(BaseModel):
    repos: list[str]  # 처리할 리포 허용 목록 "owner/name"
    redis_url: str = "redis://127.0.0.1:6379/0"
    # bare 캐시, worktree, .venv, SQLite, 검색을 켜면 chroma/와 임베딩 모델(models/)
    data_dir: Path = Path.home() / ".cache" / "pipeline"
    sync: SyncConfig = SyncConfig()
    sandbox: SandboxConfig = SandboxConfig()
    model: ModelConfig = ModelConfig()
    rag: RagConfig = RagConfig()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "pipeline.db"


def config_dir() -> Path:
    return Path(os.environ.get(CONFIG_DIR_ENV, DEFAULT_CONFIG_DIR)).expanduser()


def load_config(directory: Path | None = None) -> PipelineConfig:
    path = (directory or config_dir()) / "config.yaml"
    if not path.is_file():
        raise ConfigError(f"설정 파일이 없다: {path}")
    return PipelineConfig.model_validate(yaml.safe_load(path.read_text()) or {})


def read_secret(name: str, directory: Path | None = None) -> str:
    """비밀값 파일을 읽는다. 그룹·다른 사용자가 읽을 수 있으면 거부한다."""
    path = (directory or config_dir()) / name
    if not path.is_file():
        raise ConfigError(f"비밀값 파일이 없다: {path}")
    if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigError(f"비밀값 파일 권한이 넓다 (chmod 600 필요): {path}")
    value = path.read_text().strip()
    if not value:
        raise ConfigError(f"비밀값 파일이 비어 있다: {path}")
    return value
