"""워커 흐름 테스트 fixture: 가짜 gh·Redis·체크아웃·설정·lint. Docker·GitHub 없이 돈다."""

from contextlib import contextmanager
from pathlib import Path

import fakeredis
import pytest

from app import config
from app.models import state_keys
from app.models.finding import SkillResult
from app.worker import base_config, repo
from app.worker.deps import Deps
from app.worker.github import GitHub
from app.worker.repo import Checkout
from app.worker.skills import lint
from app.worker.state import Store

from .helpers import BASE, HEAD, VIOLATION
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
    # 가짜 체크아웃에는 git 기록이 없다. base에 규칙 문서가 없는 리포로 본다 (문서 규칙 흐름은 따로)
    monkeypatch.setattr(repo, "list_base_entries", lambda *a, **k: {})
    monkeypatch.setattr(
        lint, "run_lint", lambda *a, **k: SkillResult(skill="lint", findings=(VIOLATION,))
    )
    redis.set(state_keys.latest_head("o/r", 1), HEAD)
    return {"deps": deps, "gh": gh, "redis": redis, "monkeypatch": monkeypatch}
