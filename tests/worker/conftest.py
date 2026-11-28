"""워커 흐름 테스트 fixture: 가짜 gh·체크아웃·설정. Docker·GitHub 없이 돈다. Redis 자리는 메모리
(`env`)이고, 큐 등록·RQ를 거치는 테스트는 실제 Redis(`redis_env`)를 쓴다."""

from contextlib import contextmanager
from pathlib import Path

import pytest

from app.shared import config, state_keys_pure
from app.shared.other_server_pure import Server
from app.worker.config import base_config
from app.worker.config.access_pure import AccessSettings
from app.worker.deps import Deps
from app.worker.git import repo
from app.worker.git.github import GitHub
from app.worker.git.repo import Checkout
from app.worker.store.state import Store

from ..redis_stand_ins import MemoryKeys
from .git.test_github import FakeGh
from .helpers import BASE, HEAD, ME, ME_VERSION


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    return _env(tmp_path, monkeypatch, MemoryKeys())


@pytest.fixture
def redis_env(tmp_path: Path, monkeypatch, real_redis):
    """env와 같되 Redis 자리가 실제 Redis다 (tests/conftest.py의 real_redis)."""
    return _env(tmp_path, monkeypatch, real_redis)


def _env(tmp_path: Path, monkeypatch, redis):
    gh = FakeGh()
    deps = Deps(
        store=Store(tmp_path / "pipeline.db", redis),
        github=GitHub(gh, server=Server(ME, ME_VERSION)),
        settings=config.PipelineConfig(repos=["o/r"], data_dir=tmp_path / "data"),
    )

    @contextmanager
    def fake_checkout(data_dir, repo_name, pr_number, head_sha, base_sha, **kwargs):
        path = tmp_path / "wt"
        path.mkdir(exist_ok=True)
        # Python 프로필이 돌릴 저장소로 둔다. 환경 처방으로 가는 흐름은 tests/worker/env가 본다
        (path / "app.py").touch()
        yield Checkout(path=path, head_sha=head_sha, base_sha=base_sha, merge_base=base_sha)

    monkeypatch.setattr(repo, "checkout", fake_checkout)
    # 실제 ~/.config/pipeline의 비밀값을 읽지 않게 빈 설정 폴더를 쓴다
    monkeypatch.setenv(config.CONFIG_DIR_ENV, str(tmp_path / "config"))
    monkeypatch.setattr(base_config, "load", lambda *a, **k: BASE)
    # 가드레일(8.6)이 체크아웃 전에 bare 캐시에서 읽는 base의 `access`. 기본값으로 본다 (실제
    # 저장소로 읽는 것은 test_base_config, 설정을 바꾼 흐름은 test_guardrail)
    monkeypatch.setattr(base_config, "load_access", lambda *a, **k: AccessSettings())
    # 가짜 체크아웃에는 git 기록이 없다. base에 규칙 문서도 ruff 설정도 없는 리포로 본다 (규칙
    # 흐름은 test_doc_rules_flow·test_mechanical_flow)
    monkeypatch.setattr(repo, "list_base_entries", lambda *a, **k: {})
    redis.set(state_keys_pure.latest_head("o/r", 1), HEAD)
    return {"deps": deps, "gh": gh, "redis": redis, "monkeypatch": monkeypatch}
