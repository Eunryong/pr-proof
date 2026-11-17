"""worker/main.py: 웹훅 작업 하나가 체크아웃 → 테스트 → 코멘트까지 가는 흐름. Docker·GitHub 없이."""

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Final

import fakeredis
import pytest

from app import config
from app.models import state_keys
from app.models.job import PRJob
from app.models.test_run import PytestRun, ReportSummary
from app.sandbox.docker_cli import SandboxError
from app.sandbox.venv import DependencyInstallError
from app.worker import graph, main, repo
from app.worker.github import GitHub
from app.worker.repo import Checkout
from app.worker.state import Store

from .test_github import FakeGh

HEAD: Final = "a" * 40
PASSED: Final = PytestRun(exit_code=0, summary=ReportSummary(passed=9, total=9))


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
    redis.set(state_keys.latest_head("o/r", 1), HEAD)
    return {"deps": deps, "gh": gh, "redis": redis, "monkeypatch": monkeypatch}


def _payload(kind: str = "update", **extra) -> str:
    fields = {"repo": "o/r", "pr_number": 1, "kind": kind, "source": "webhook", **extra}
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
    assert "진행 중 (체크아웃)" in bodies[0] and "진행 중 (테스트 실행)" in bodies[1]
    assert "**테스트:** 통과 (9개)" in bodies[-1]
    # 첫 코멘트 뒤에는 같은 코멘트를 고친다
    assert env["gh"].kinds() == ["find", "create", "patch", "patch"]
    assert env["deps"].store.last_comment_id("o/r", 1) == 555


def test_job_key_is_released_after_success(env) -> None:
    _runner(env, lambda *a, **k: PASSED)
    job = PRJob.model_validate_json(_payload())
    env["redis"].set(state_keys.job(job.dedup_key), "1")
    main.handle(_payload(), env["deps"])
    assert env["redis"].get(state_keys.job(job.dedup_key)) is None


def test_stale_job_does_nothing(env) -> None:
    env["redis"].set(state_keys.latest_head("o/r", 1), "c" * 40)
    assert main.handle(_payload(), env["deps"]) == "stale"
    assert env["gh"].calls == []


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


def test_last_environment_retry_says_not_checked(env) -> None:
    _runner(env, lambda *a, **k: (_ for _ in ()).throw(SandboxError("x")))
    env["monkeypatch"].setattr(main, "_attempt", lambda: (4, True))
    with pytest.raises(SandboxError):
        main.handle(_payload(), env["deps"])
    assert "환경 문제로 검사하지 못함" in _bodies(env["gh"])[-1]


def test_resolution_failure_is_unrunnable_at_once(env) -> None:
    def no_package(*args, **kwargs):
        raise DependencyInstallError("resolution", "key", "No matching distribution found for x")

    _runner(env, no_package)
    assert main.handle(_payload(), env["deps"]) == "unrunnable"
    assert "실행 불가 (의존성을 설치하지 못함)" in _bodies(env["gh"])[-1]


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
