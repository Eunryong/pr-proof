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
from pydantic import BaseModel

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


class PipelineConfig(BaseModel):
    repos: list[str]  # 처리할 리포 허용 목록 "owner/name"
    redis_url: str = "redis://127.0.0.1:6379/0"
    data_dir: Path = Path.home() / ".cache" / "pipeline"  # bare 캐시, worktree, .venv, SQLite
    sync: SyncConfig = SyncConfig()

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
