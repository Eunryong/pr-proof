"""워커 작업이 쓰는 의존 객체. 테스트에서는 가짜를 넣는다."""

from dataclasses import dataclass, field

from redis import Redis

from app.shared import config, server
from app.worker.config.repo_config_pure import ForcedModel
from app.worker.git.github import GitHub
from app.worker.model import backends, usage_fallback
from app.worker.model.model_backend import ModelBackend
from app.worker.store.state import Store


@dataclass
class Deps:
    store: Store
    github: GitHub
    settings: config.PipelineConfig
    # 모델 구현 (`app/worker/model/model_backend.py`). 호출하는 쪽은 이것만 안다
    backend: ModelBackend = field(default_factory=lambda: backends.default_backend())

    @classmethod
    def from_config(cls) -> "Deps":
        settings = config.load_config()
        redis = Redis.from_url(settings.redis_url)
        github = GitHub(server=server.current(settings.data_dir))
        backend, fallen = usage_fallback.active(
            settings.model, settings.data_dir, backends.from_config(settings.model)
        )
        if fallen and settings.model.fallback is not None:
            # 모든 단계를 로컬 모델의 이름으로 돌려, 저장한 답의 키에 실제로 답한 모델이
            # 들어가게 한다
            forced = ForcedModel(settings.model.fallback.model)
            settings = settings.model_copy(
                update={"model": settings.model.model_copy(update={"default": forced})}
            )
        return cls(Store(settings.db_path, redis), github, settings, backend)
