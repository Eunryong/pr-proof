"""환경 준비(`plan_env`)가 그래프에서 도는지: Python 프로필이 돌릴 것이 없는 저장소만 처방으로 가고,
처방이 없으면 그 까닭이 테스트 줄에 보인다. 실제 git 체크아웃과 SQLite로 돌고 모델과 Docker는 쓰지
않는다. 처방을 굽고 돌리는 흐름은 test_env_flow_docker.py가 본다."""

from pathlib import Path
from typing import Final

import pytest
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.worker import flow_nodes, graph
from app.worker.config.repo_config_pure import CallSettings, ModelSettings, RepoConfig
from app.worker.env.install import Limits
from app.worker.env.use_pure import EnvPlanned, EnvSkipped
from app.worker.testing.test_run_pure import EnvUse, StepRun, TestRun

from ..helpers import bodies
from ..rules.test_doc_rules_flow import check, doc_env, use_config  # noqa: F401

CPP_BASE: Final = {"CMakeLists.txt": "project(x)\n", "src/a.cpp": "int a() { return 1; }\n"}
CPP_PR: Final = {"src/a.cpp": "int a() { return 2; }\n"}
REAL_RUN: Final = graph.run_pr_tests
PY_PASSED: Final = TestRun(test=StepRun("pytest", 0), verdict="passed")


@pytest.fixture
def cpp_env(doc_env):  # noqa: F811
    """doc_env의 테스트를 진짜 run_pr_tests로 되돌린다(처방 길을 지나게). Docker 상한은 고정 값."""
    doc_env["monkeypatch"].setattr(graph, "run_pr_tests", REAL_RUN)
    doc_env["monkeypatch"].setattr(flow_nodes, "default_limits", lambda: Limits(1, 1, 60, 60))
    return doc_env


def test_a_repository_without_python_shows_why_no_recipe_was_made(cpp_env, tmp_path: Path) -> None:
    """처방 정하기의 호출 상한이 0이면 모델도 Docker도 부르지 않고 그 까닭이 테스트 줄에 보인다."""
    use_config(cpp_env, RepoConfig(model=ModelSettings(calls=CallSettings(ruleset_base=0))))
    state = check(cpp_env, tmp_path, CPP_BASE, CPP_PR)
    assert isinstance(state["env"], EnvSkipped)
    run = state["test_run"]
    assert run.status == "no_env" and run.unplanned.startswith("환경 처방을 정하는 모델 호출이")
    posted = bodies(cpp_env["gh"])
    assert any("진행 중 (환경 준비)" in body for body in posted)
    assert f"**테스트:** 돌리지 않음 ({run.unplanned})" in posted[-1]
    assert "Python뿐" not in posted[-1]


def test_a_python_repository_does_not_prepare_an_environment(cpp_env, tmp_path: Path) -> None:
    cpp_env["monkeypatch"].setattr(graph, "run_pr_tests", lambda *a, **k: PY_PASSED)
    state = check(cpp_env, tmp_path, {"a.py": "x = 1\n"}, {"a.py": "x = 2\n"})
    assert "env" not in state and state["test_run"] == PY_PASSED
    assert not any("환경 준비" in body for body in bodies(cpp_env["gh"]))


def test_environment_references_survive_the_checkpoint() -> None:
    serde = JsonPlusSerializer(allowed_msgpack_modules=list(graph._STATE_TYPES))
    run = TestRun(build=(StepRun("build", 0),), env=EnvUse(3, (("system", True),), "head", ("a",)))
    values = {"env": EnvPlanned(3), "skipped": EnvSkipped("x"), "test_run": run}
    restored = serde.loads_typed(serde.dumps_typed(values))
    assert restored["env"] == EnvPlanned(3) and restored["skipped"] == EnvSkipped("x")
    use = restored["test_run"].env
    assert isinstance(use, EnvUse) and use.plan_id == 3 and use.inputs == "head"
