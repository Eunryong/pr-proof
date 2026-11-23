"""워커 작업이 쓰는 의존 객체. 테스트에서는 가짜를 넣는다."""

from dataclasses import dataclass, field

from redis import Redis

from app.shared import config
from app.worker.git.github import GitHub
from app.worker.model import backends
from app.worker.model.model_backend import ModelBackend
from app.worker.store.state import Store


@dataclass
class Deps:
    store: Store
    github: GitHub
    settings: config.PipelineConfig
    # 모델 구현 (`app/worker/model/model_backend.py`). 호출하는 쪽은 이것만 안다
    backend: ModelBackend = field(default_factory=backends.default_backend)

    @classmethod
    def from_config(cls) -> "Deps":
        settings = config.load_config()
        redis = Redis.from_url(settings.redis_url)
        return cls(Store(settings.db_path, redis), GitHub(), settings, backends.default_backend())
