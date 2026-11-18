"""워커 작업이 쓰는 의존 객체. 테스트에서는 가짜를 넣는다."""

from dataclasses import dataclass

from redis import Redis

from app import config
from app.worker.github import GitHub
from app.worker.state import Store


@dataclass
class Deps:
    store: Store
    github: GitHub
    settings: config.PipelineConfig

    @classmethod
    def from_config(cls) -> "Deps":
        settings = config.load_config()
        redis = Redis.from_url(settings.redis_url)
        return cls(Store(settings.db_path, redis), GitHub(), settings)
