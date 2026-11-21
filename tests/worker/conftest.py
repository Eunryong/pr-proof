"""워커 흐름 테스트 fixture: 가짜 gh·Redis·체크아웃·설정. Docker·GitHub 없이 돈다."""

from contextlib import contextmanager
from pathlib import Path

import fakeredis
import pytest

from app.shared import config
from app.shared import state_keys_pure as state_keys
from app.worker import base_config, repo
from app.worker.access_pure import AccessSettings
from app.worker.deps import Deps
from app.worker.github import GitHub
from app.worker.repo import Checkout
from app.worker.state import Store

from .helpers import BASE, HEAD
from .test_github import FakeGh


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    redis = fakeredis.FakeStrictRedis()
    gh = FakeGh()
    deps = Deps(
        store=Store(tmp_path / "pipeline.db", redis),
        github=GitHub(gh),
        settings=config.PipelineConfig(repos=["o/r"], data_dir=tmp_path / "data"),
    )

    @contextmanager
    def fake_checkout(data_dir, repo_name, pr_number, head_sha, base_sha, **kwargs):
        path = tmp_path / "wt"
        path.mkdir(exist_ok=True)
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
    redis.set(state_keys.latest_head("o/r", 1), HEAD)
    return {"deps": deps, "gh": gh, "redis": redis, "monkeypatch": monkeypatch}
