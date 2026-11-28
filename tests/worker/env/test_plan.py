"""app/worker/env/plan.py: 저장한 처방을 다시 쓰는 길과 호출 상한. 모델과 Docker를 쓰지 않는다.

처방을 새로 정하는 길은 실제 모델이 필요하다(`test_plan_real_cli.py`). 여기서는 저장된 처방과 실제
git 저장소로, 모델에 닿기 전에 끝나는 경우만 본다. read_code는 불리면 테스트를 실패시킨다.
"""

import json
import subprocess
from pathlib import Path

import pytest

from app.shared.db_pure import EnvPlanEntry
from app.worker.config.repo_config_pure import CallSettings
from app.worker.env.install import Limits
from app.worker.env.plan import EnvReady, EnvUnplanned, plan_environment
from app.worker.env.plan_pure import PROMPT_VERSION, BaseRun, base_run_to_json
from app.worker.git import repo
from app.worker.store.budget import CallBudget
from app.worker.store.state import Store

from ...redis_stand_ins import MemoryKeys

RECIPE = {
    "system": {"image": "debian:bookworm-slim", "commands": ["apt-get install -y make"]},
    "build": [{"name": "build", "command": "make"}],
}
LIMITS = Limits(memory_gb=1, cpus=1, install_timeout_s=60, step_timeout_s=60)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip


def _commit(clone: Path, files: dict[str, str]) -> str:
    for name, text in files.items():
        (clone / name).write_text(text)
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "c")
    return _git(clone, "rev-parse", "HEAD")


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q")
    _commit(path, {"Makefile": "all:\n\ttrue\n", "README.md": "make\n"})
    repo.record_git_dir(path)
    return path


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "pipeline.db", MemoryKeys())


def _never(worktree: Path):  # noqa: ANN202
    raise AssertionError("저장한 처방을 쓰거나 상한에 걸린 경우에는 모델 폴더를 열지 않는다")


def _plan(clone: Path, store: Store, base: str, *, model: str = "sonnet", cap: int = 0):  # noqa: ANN202
    budget = CallBudget(store, "o/r", 0, base, CallSettings(ruleset_base=cap))
    return plan_environment(clone, base, store=store, budget=budget, model=model,
                            read_code=_never, limits=LIMITS)  # fmt: skip


def _save(store: Store, clone: Path, base: str, **fields: str) -> int:
    reads = {"Makefile": _git(clone, "rev-parse", f"{base}:Makefile")}
    entry = {"repo": "o/r", "model": "sonnet", "prompt_version": PROMPT_VERSION,
             "recipe_json": json.dumps(RECIPE), "reads_json": json.dumps(reads),
             "base_run_json": base_run_to_json(BaseRun("passed", (), None)), **fields}  # fmt: skip
    return store.save_env_plan(EnvPlanEntry(**entry))


def test_stored_plan_is_reused_while_its_reads_are_unchanged(clone: Path, store: Store) -> None:
    base = _git(clone, "rev-parse", "HEAD")
    plan_id = _save(store, clone, base)
    later = _commit(clone, {"README.md": "다른 설명\n"})  # 처방이 기대지 않은 파일만 바뀜
    got = _plan(clone, store, later)
    assert isinstance(got, EnvReady) and got.cached and got.plan_id == plan_id
    assert [s.name for s in got.recipe.build] == ["build"]
    calls = store.model_calls("o/r", 0)
    assert [(c.stage, c.cache_hit, c.counted, c.ruleset_key) for c in calls] == [
        ("env", True, 0, "env:o/r")
    ]


def test_newest_fresh_plan_wins(clone: Path, store: Store) -> None:
    base = _git(clone, "rev-parse", "HEAD")
    _save(store, clone, base)
    newer = _save(store, clone, base)
    got = _plan(clone, store, base)
    assert isinstance(got, EnvReady) and got.plan_id == newer


@pytest.mark.parametrize(
    "change",
    [
        "read_changed",
        "read_removed",
        "other_model",
        "old_prompt",
        "broken_row",
    ],
)
def test_stale_plan_is_not_reused(clone: Path, store: Store, change: str) -> None:
    """다시 쓰지 못하면 새로 정하러 간다. 상한이 0이라 모델 폴더를 열기 전에 멈춘다."""
    base = _git(clone, "rev-parse", "HEAD")
    fields = {"other_model": {"model": "opus"}, "old_prompt": {"prompt_version": "old"},
              "broken_row": {"reads_json": "["}}.get(change, {})  # fmt: skip
    _save(store, clone, base, **fields)
    if change == "read_changed":
        base = _commit(clone, {"Makefile": "all:\n\tfalse\n"})
    elif change == "read_removed":
        _git(clone, "rm", "-q", "Makefile")
        _git(clone, "commit", "-q", "-m", "rm")
        base = _git(clone, "rev-parse", "HEAD")
    got = _plan(clone, store, base, cap=0)
    assert isinstance(got, EnvUnplanned) and got.kind == "cap"
    assert "환경 처방 상한" in got.reason
    assert [c.cache_hit for c in store.model_calls("o/r", 0)] == []
