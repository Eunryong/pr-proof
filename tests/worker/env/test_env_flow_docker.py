"""처방으로 도는 PR 흐름의 통합 테스트: 저장한 작은 처방(env_plans의 데이터)으로 실제 run_pr_tests가
층을 굽고 작은 저장소의 PR을 빌드·테스트하고, 결과 코멘트에 환경 블록이 보인다.

모델은 부르지 않는다(처방은 저장한 것을 다시 쓰고, 다른 모델 단계는 규칙 문서가 없어 돌지 않는다).
시작 이미지는 debian:bookworm-slim이고 큰 것을 받지 않는다.
"""

import json
import subprocess
from pathlib import Path
from typing import Final

import pytest
from sqlmodel import Session, select

from app.shared import state_keys_pure
from app.shared.db_pure import EnvPlanEntry, ModelCall
from app.shared.job_pure import PRJob
from app.worker import flow_nodes, graph, update
from app.worker.config.access_pure import Author
from app.worker.env import use
from app.worker.env.install import Limits
from app.worker.env.plan_pure import PROMPT_VERSION, BaseRun, base_run_to_json
from app.worker.env.use_pure import EnvPlanned
from app.worker.report.env_report_pure import SUMMARY
from tests.sandbox_names import TEST_NAMES

from ..helpers import bodies
from ..rules.test_doc_rules_flow import doc_env  # noqa: F401

pytestmark = pytest.mark.docker

LIMITS: Final = Limits(memory_gb=1, cpus=1, install_timeout_s=300, step_timeout_s=60)
JUNIT: Final = ".pipeline-out/junit.xml"
PASSING: Final = '<testsuite><testcase classname="t" name="one"/><testcase classname="t" name="two"/></testsuite>'  # noqa: E501
BASE_FILES: Final = {
    "build.sh": "grep -q '^ok$' src.txt || { echo 'src.txt:1: error: expected ok'; exit 2; }\n",
    "test.sh": f"mkdir -p .pipeline-out && printf '%s' '{PASSING}' > {JUNIT}\n",
    "src.txt": "ok\n",
    "dep.txt": "d1\n",
}
RECIPE: Final = {
    "system": {"image": "debian:bookworm-slim", "commands": ["true"]},
    "deps": {"inputs": ["dep.txt"], "commands": ["cp /opt/env/src/dep.txt /opt/env/dep.txt"]},
    "build": [{"name": "build", "command": "sh build.sh"}],
    "test": {"name": "test", "command": "sh test.sh", "report": "junit", "report_path": JUNIT},
}
GREEN: Final = BaseRun("passed", (), None)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip


@pytest.fixture
def remote(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "remote"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "uploadpack.allowAnySHA1InWant", "true")
    for name, text in BASE_FILES.items():
        (path / name).write_text(text)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "base")
    return path, _git(path, "rev-parse", "HEAD")


@pytest.fixture
def flow_env(doc_env, remote):  # noqa: F811
    """doc_env의 테스트를 진짜 run_pr_tests로 되돌리고, base에서 확인한 처방을 저장해 둔다."""
    path, base = remote
    mp = doc_env["monkeypatch"]
    mp.setattr(graph, "run_pr_tests", REAL_RUN)
    mp.setattr(flow_nodes, "default_limits", lambda: LIMITS)
    mp.setattr(use, "default_limits", lambda: LIMITS)
    reads = {name: _git(path, "rev-parse", f"{base}:{name}") for name in ("build.sh", "dep.txt")}
    doc_env["plan_id"] = doc_env["deps"].store.save_env_plan(EnvPlanEntry(
        repo="o/r", model="sonnet", prompt_version=PROMPT_VERSION, recipe_json=json.dumps(RECIPE),
        reads_json=json.dumps(reads), base_run_json=base_run_to_json(GREEN),
    ))  # fmt: skip
    return doc_env


REAL_RUN: Final = graph.run_pr_tests


def _pr(env, remote: tuple[Path, str], number: int, files: dict[str, str],
        author: Author | None = None):  # fmt: skip
    """base 위의 한 커밋을 PR number로 그래프에 돌린 상태와 마지막 코멘트."""
    path, base = remote
    _git(path, "checkout", "-q", "-B", f"pr{number}", base)
    for name, text in files.items():
        (path / name).write_text(text)
    _git(path, "commit", "-q", "-am", f"pr{number}")
    head = _git(path, "rev-parse", "HEAD")
    _git(path, "update-ref", f"refs/pull/{number}/head", head)
    env["redis"].set(state_keys_pure.latest_head("o/r", number), head)
    job = PRJob(repo="o/r", pr_number=number, kind="update", head_sha=head, base_sha=base,
                source="webhook")  # fmt: skip
    deps = env["deps"]
    ctx = update.flow_context(deps, deps.store.start_run(job), None, author)
    ctx.sandbox_names = TEST_NAMES
    state = graph.run_flow(job, ctx)
    return state, bodies(env["gh"])[-1]


def _env_calls(env) -> list[ModelCall]:
    with Session(env["deps"].store.engine) as session:
        return list(session.exec(select(ModelCall).where(ModelCall.stage == "env")))


def test_a_pr_builds_and_tests_with_the_stored_recipe(flow_env, remote) -> None:
    """믿지 않는 작성자가 의존성 입력을 바꿨다: base의 입력으로 굽고, 결과에 그 사실이 보인다."""
    state, body = _pr(flow_env, remote, 1, {"dep.txt": "d2\n"})
    assert state["env"] == EnvPlanned(flow_env["plan_id"])
    run = state["test_run"]
    assert run.status == "passed", run.output
    assert (run.report.total, run.report.passed) == (2, 2)
    assert run.env.inputs == "base" and run.env.changed_inputs == ("dep.txt",)
    assert "**테스트:** 통과 (2개)\n" in body
    assert f"<details><summary>{SUMMARY}</summary>" in body
    assert "PR이 바꾼 `dep.txt`는 쓰지 않음 (신뢰된 작성자가 아님)" in body
    # 처방은 저장한 것을 다시 썼다: 센 호출이 없다
    assert [(c.counted, c.cache_hit) for c in _env_calls(flow_env)] == [(0, True)]


def test_a_pr_that_breaks_the_build_shows_the_failing_step(flow_env, remote) -> None:
    state, body = _pr(flow_env, remote, 2, {"src.txt": "broken\n"})
    run = state["test_run"]
    assert run.status == "build_failed" and run.failed_build.name == "build"
    assert "src.txt:1: error: expected ok" in run.output
    assert "**테스트:** 빌드 실패로 테스트를 돌리지 못함 (`build` 단계, 종료 코드 2)\n" in body
    assert "src.txt:1: error: expected ok" in body
    assert all(c.counted == 0 for c in _env_calls(flow_env))


def test_a_trusted_author_bakes_the_changed_inputs(flow_env, remote) -> None:
    state, body = _pr(flow_env, remote, 3, {"dep.txt": "d3\n"}, Author("MEMBER", "member"))
    run = state["test_run"]
    assert run.status == "passed", run.output
    assert run.env.inputs == "head" and run.env.changed_inputs == ("dep.txt",)
    assert "의존성 입력 (PR의 파일 (신뢰된 작성자)): `dep.txt`" in body
