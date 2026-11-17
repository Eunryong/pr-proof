"""worker/main.py: 웹훅 작업 하나가 체크아웃 → 테스트 → 코멘트까지 가는 흐름. Docker·GitHub 없이."""

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Final

import fakeredis
import pytest
from rq.timeouts import JobTimeoutException
from sqlmodel import Session, select

from app import config
from app.models import state_keys
from app.models.db import Run
from app.models.finding import Finding, SkillResult
from app.models.job import PRJob
from app.models.repo_config import RepoConfig
from app.models.test_run import PytestRun, ReportSummary
from app.sandbox.docker_cli import SandboxError
from app.sandbox.venv import DependencyInstallError
from app.worker import base_config, graph, main, repo
from app.worker.base_config import BaseConfig
from app.worker.github import GitHub, GitHubError
from app.worker.repo import Checkout
from app.worker.skills import lint
from app.worker.state import Store

from .test_github import FakeGh

HEAD: Final = "a" * 40
PASSED: Final = PytestRun(exit_code=0, summary=ReportSummary(passed=9, total=9))
BASE: Final = BaseConfig(
    repo=RepoConfig(), memory_gb=2, max_memory_gb=4, ruff_config=None, ruff_source="기본 규칙"
)
VIOLATION: Final = Finding(
    file="a.py", line=1, severity="error", message="m", skill="lint", source="x", code="F401"
)


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    redis = fakeredis.FakeStrictRedis()
    gh = FakeGh()
    deps = main.Deps(
        store=Store(tmp_path / "pipeline.db", redis),
        github=GitHub(gh),
        settings=config.PipelineConfig(repos=["o/r"], data_dir=tmp_path / "data"),
    )

    @contextmanager
    def fake_checkout(data_dir, repo_name, pr_number, head_sha, base_sha, **kwargs):
        path = tmp_path / "wt"
        path.mkdir(exist_ok=True)
        yield Checkout(path=path, head_sha=head_sha, base_sha=base_sha)

    monkeypatch.setattr(repo, "checkout", fake_checkout)
    monkeypatch.setattr(base_config, "load", lambda *a, **k: BASE)
    monkeypatch.setattr(
        lint, "run_lint", lambda *a, **k: SkillResult(skill="lint", findings=(VIOLATION,))
    )
    redis.set(state_keys.latest_head("o/r", 1), HEAD)
    return {"deps": deps, "gh": gh, "redis": redis, "monkeypatch": monkeypatch}


def _payload(kind: str = "update", **extra) -> str:
    fields = {"repo": "o/r", "pr_number": 1, "kind": kind, "source": "webhook"} | extra
    if kind != "command":
        fields.setdefault("head_sha", HEAD)
        fields.setdefault("base_sha", "b" * 40)
    return PRJob(**fields).model_dump_json()


def _runner(env, fn) -> None:
    env["monkeypatch"].setattr(graph, "run_pr_tests", fn)


def _bodies(gh: FakeGh) -> list[str]:
    return [json.loads(stdin)["body"] for args, stdin in gh.calls if stdin]


def test_passed_run_posts_progress_then_result(env) -> None:
    _runner(env, lambda *a, **k: PASSED)
    assert main.handle(_payload(), env["deps"]) == "passed"
    bodies = _bodies(env["gh"])
    assert "진행 중 (체크아웃)" in bodies[0]
    assert "진행 중 (규칙 검사)" in bodies[1] and "진행 중 (테스트 실행)" in bodies[2]
    assert "**테스트:** 통과 (9개)" in bodies[-1]
    assert "**규칙:** ⚠️ 위반 1건 (lint 1)" in bodies[-1]
    # 첫 코멘트 뒤에는 같은 코멘트를 고친다
    assert env["gh"].kinds() == ["find", "create", "patch", "patch", "patch"]
    assert env["deps"].store.last_comment_id("o/r", 1) == 555


def test_job_key_is_released_after_success(env) -> None:
    _runner(env, lambda *a, **k: PASSED)
    job = PRJob.model_validate_json(_payload())
    env["redis"].set(state_keys.job(job.dedup_key), "1")
    main.handle(_payload(), env["deps"])
    assert env["redis"].get(state_keys.job(job.dedup_key)) is None


def _states(env) -> list[str]:
    with Session(env["deps"].store.engine) as session:
        return [r.state for r in session.exec(select(Run)).all()]


def test_stale_job_does_nothing(env) -> None:
    env["gh"].head = "c" * 40
    assert main.handle(_payload(), env["deps"]) == "stale"
    assert env["gh"].kinds() == []  # 코멘트를 건드리지 않는다
    assert _states(env) == []


def test_late_webhook_for_old_commit_restores_latest_head(env) -> None:
    """새 커밋 c의 웹훅 뒤에 옛 커밋 a의 웹훅이 늦게 와서 Redis가 a로 덮였다."""
    env["gh"].head = "c" * 40
    env["redis"].set(state_keys.latest_head("o/r", 1), HEAD)
    _runner(env, lambda *a, **k: pytest.fail("옛 커밋을 검사하면 안 된다"))
    assert main.handle(_payload(), env["deps"]) == "stale"
    assert env["redis"].get(state_keys.latest_head("o/r", 1)).decode() == "c" * 40


def test_current_commit_runs_even_if_redis_was_overwritten(env) -> None:
    """반대 순서: 새 커밋의 작업이 먼저 꺼내졌는데 Redis에는 늦게 온 옛 커밋이 적혀 있다."""
    env["redis"].set(state_keys.latest_head("o/r", 1), "0" * 40)
    _runner(env, lambda *a, **k: PASSED)
    assert main.handle(_payload(), env["deps"]) == "passed"
    assert env["redis"].get(state_keys.latest_head("o/r", 1)).decode() == HEAD


@pytest.mark.parametrize("final", [False, True])
def test_head_lookup_failure_is_environment_error(env, final: bool) -> None:
    env["gh"].head_error = "gh: 401"
    env["monkeypatch"].setattr(main, "_attempt", lambda: (4 if final else 1, final))
    job = PRJob.model_validate_json(_payload())
    env["redis"].set(state_keys.job(job.dedup_key), "1")
    with pytest.raises(GitHubError):
        main.handle(_payload(), env["deps"])
    assert _states(env) == []
    assert (env["redis"].get(state_keys.job(job.dedup_key)) is None) == final


@pytest.mark.parametrize(
    "error", [RuntimeError("버그"), JobTimeoutException("30분 초과")], ids=["bug", "job-timeout"]
)
def test_unexpected_error_is_recorded_and_retried(env, error: Exception) -> None:
    _runner(env, lambda *a, **k: (_ for _ in ()).throw(error))
    env["monkeypatch"].setattr(main, "_attempt", lambda: (1, False))
    job = PRJob.model_validate_json(_payload())
    env["redis"].set(state_keys.job(job.dedup_key), "1")
    with pytest.raises(type(error)):
        main.handle(_payload(), env["deps"])
    assert _states(env) == ["error"]  # running으로 남으면 동기화가 죽은 워커로 보고 계속 넣는다
    body = _bodies(env["gh"])[-1]
    assert "파이프라인 오류로 대기 중" in body and "실패" not in body
    assert env["redis"].get(state_keys.job(job.dedup_key)) is not None  # 재시도 동안 키 유지


def test_last_unexpected_error_releases_key(env) -> None:
    _runner(env, lambda *a, **k: (_ for _ in ()).throw(RuntimeError("버그")))
    env["monkeypatch"].setattr(main, "_attempt", lambda: (4, True))
    job = PRJob.model_validate_json(_payload())
    env["redis"].set(state_keys.job(job.dedup_key), "1")
    with pytest.raises(RuntimeError):
        main.handle(_payload(), env["deps"])
    assert "파이프라인 오류로 검사하지 못함" in _bodies(env["gh"])[-1]
    assert env["redis"].get(state_keys.job(job.dedup_key)) is None


def test_commit_that_goes_stale_during_tests_leaves_no_result(env) -> None:
    def run_then_new_commit(*args, **kwargs):
        env["redis"].set(state_keys.latest_head("o/r", 1), "c" * 40)
        return PASSED

    _runner(env, run_then_new_commit)
    assert main.handle(_payload(), env["deps"]) == "stale"
    assert not any("통과" in b for b in _bodies(env["gh"]))  # 새 커밋의 작업이 코멘트를 고친다


def test_environment_error_shows_wait_and_raises_for_retry(env) -> None:
    def broken(*args, **kwargs):
        raise SandboxError("docker 중단")

    _runner(env, broken)
    env["monkeypatch"].setattr(main, "_attempt", lambda: (1, False))
    job = PRJob.model_validate_json(_payload())
    env["redis"].set(state_keys.job(job.dedup_key), "1")
    with pytest.raises(SandboxError):
        main.handle(_payload(), env["deps"])
    body = _bodies(env["gh"])[-1]
    assert "환경 문제로 대기 중" in body and "실패" not in body
    assert env["redis"].get(state_keys.job(job.dedup_key)) is not None  # 재시도 동안 키 유지


def test_last_environment_retry_says_not_checked_and_releases_key(env) -> None:
    _runner(env, lambda *a, **k: (_ for _ in ()).throw(SandboxError("x")))
    env["monkeypatch"].setattr(main, "_attempt", lambda: (4, True))
    job = PRJob.model_validate_json(_payload())
    env["redis"].set(state_keys.job(job.dedup_key), "1")
    with pytest.raises(SandboxError):
        main.handle(_payload(), env["deps"])
    assert "환경 문제로 검사하지 못함" in _bodies(env["gh"])[-1]
    # 동기화가 30분 뒤 같은 커밋을 다시 넣을 수 있어야 한다
    assert env["redis"].get(state_keys.job(job.dedup_key)) is None


def test_resolution_failure_is_unrunnable_at_once(env) -> None:
    def no_package(*args, **kwargs):
        raise DependencyInstallError("resolution", "key", "No matching distribution found for x")

    _runner(env, no_package)
    assert main.handle(_payload(), env["deps"]) == "unrunnable"
    body = _bodies(env["gh"])[-1]
    assert "실행 불가 (의존성을 설치하지 못함)" in body
    assert "**규칙:** ⚠️ 위반 1건" in body  # 규칙 검사는 설치와 따로 돈다


def test_tests_get_repo_memory_limit(env) -> None:
    seen = {}

    def runner(*args, **kwargs):
        seen.update(kwargs)
        return PASSED

    _runner(env, runner)
    main.handle(_payload(), env["deps"])
    assert seen["memory_gb"] == BASE.memory_gb


def test_commit_that_goes_stale_during_lint_stops(env) -> None:
    env["monkeypatch"].setattr(lint, "run_lint", lambda *a, **k: None)  # 취소됨
    _runner(env, lambda *a, **k: pytest.fail("테스트까지 가면 안 된다"))
    assert main.handle(_payload(), env["deps"]) == "stale"


@pytest.mark.parametrize(("attempt", "unrunnable"), [(1, False), (2, True)])
def test_unknown_install_failure_uses_retry_count(env, attempt: int, unrunnable: bool) -> None:
    def build_failure(*args, **kwargs):
        raise DependencyInstallError("unknown", "key", "Failed building wheel")

    _runner(env, build_failure)
    env["monkeypatch"].setattr(main, "_attempt", lambda: (attempt, False))
    if unrunnable:
        assert main.handle(_payload(), env["deps"]) == "unrunnable"
    else:
        with pytest.raises(DependencyInstallError):
            main.handle(_payload(), env["deps"])
        assert "환경 문제로 대기 중" in _bodies(env["gh"])[-1]


def test_command_job_is_not_handled_yet(env) -> None:
    payload = _payload("command", command="/recheck", comment_id=9)
    assert main.handle(payload, env["deps"]) == "command_not_supported"
    assert env["gh"].calls == []


def test_sync_job_result_says_so(env) -> None:
    _runner(env, lambda *a, **k: PASSED)
    main.handle(_payload(source="sync"), env["deps"])
    assert "동기화로 처리됨 (웹훅 누락)" in _bodies(env["gh"])[-1]
