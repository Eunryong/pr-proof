"""app/worker/main.py·update.py: update 작업이 체크아웃부터 코멘트까지 가는 흐름. Docker·GitHub
없이."""

from pathlib import Path

import fakeredis
import pytest
from rq.timeouts import JobTimeoutException
from sqlmodel import Session, select

from app.shared import config, state_keys_pure
from app.shared.db_pure import Run
from app.shared.job_pure import PRJob
from app.shared.ops_alert_pure import TEXT
from app.worker import main, preflight, retry, update
from app.worker.doc_rules_pure import NoRuleDocs
from app.worker.github import GitHubError
from app.worker.lang.python import DependencyInstallError
from app.worker.rules import doc_rules
from app.worker.sandbox.docker_cli import SandboxError

from .helpers import BASE, HEAD, PASSED, bodies, use_runner


def _payload(kind: str = "update", **extra) -> str:
    fields = {"repo": "o/r", "pr_number": 1, "kind": kind, "source": "webhook"} | extra
    if kind != "command":
        fields.setdefault("head_sha", HEAD)
        fields.setdefault("base_sha", "b" * 40)
    return PRJob(**fields).model_dump_json()


def test_passed_run_posts_progress_then_result(env) -> None:
    use_runner(env, lambda *a, **k: PASSED)
    assert main.handle(_payload(), env["deps"]) == "passed"
    posted = bodies(env["gh"])
    assert "진행 중 (체크아웃)" in posted[0]
    assert "진행 중 (규칙 읽기)" in posted[1]
    assert "진행 중 (테스트 실행)" in posted[2]
    assert "**테스트:** 통과 (9개)" in posted[-1]
    # 규칙 문서도 ruff 설정도 없는 base다. 파이프라인이 정한 규칙으로 검사하지 않는다 (2.2)
    assert "**규칙:** 규칙을 찾지 못해 규칙 검사를 하지 않음" in posted[-1]
    # 첫 코멘트 뒤에는 같은 코멘트를 고친다
    assert env["gh"].kinds() == ["find", "create", "patch", "patch", "patch"]
    assert env["deps"].store.last_comment_id("o/r", 1) == 555


def test_job_key_is_released_after_success(env) -> None:
    use_runner(env, lambda *a, **k: PASSED)
    job = PRJob.model_validate_json(_payload())
    env["redis"].set(state_keys_pure.job(job.dedup_key), "1")
    main.handle(_payload(), env["deps"])
    assert env["redis"].get(state_keys_pure.job(job.dedup_key)) is None


def _states(env) -> list[str]:
    with Session(env["deps"].store.engine) as session:
        return [r.state for r in session.exec(select(Run)).all()]


def test_stale_job_does_nothing(env) -> None:
    env["gh"].head = "c" * 40
    assert main.handle(_payload(), env["deps"]) == "stale"
    assert env["gh"].kinds() == []  # 코멘트를 건드리지 않는다
    assert _states(env) == []


def test_closed_pr_is_not_checked(env) -> None:
    """닫히기 전에 등록된 작업이 닫힌 뒤에 실행된다 (워커가 죽어 RQ가 나중에 다시 실행한 경우)."""
    env["gh"].state = "closed"
    use_runner(env, lambda *a, **k: pytest.fail("닫힌 PR을 검사하면 안 된다"))
    assert main.handle(_payload(), env["deps"]) == "closed"
    assert env["gh"].kinds() == []  # 코멘트도 제안 PR도 건드리지 않는다
    assert _states(env) == []
    # 닫힘 표시를 head로 덮지 않는다. 덮으면 돌고 있던 작업이 멈추지 않는다
    assert env["redis"].get(state_keys_pure.latest_head("o/r", 1)).decode() == update.CLOSED_HEAD


def test_late_webhook_for_old_commit_restores_latest_head(env) -> None:
    """새 커밋 c의 웹훅 뒤에 옛 커밋 a의 웹훅이 늦게 와서 Redis가 a로 덮였다."""
    env["gh"].head = "c" * 40
    env["redis"].set(state_keys_pure.latest_head("o/r", 1), HEAD)
    use_runner(env, lambda *a, **k: pytest.fail("옛 커밋을 검사하면 안 된다"))
    assert main.handle(_payload(), env["deps"]) == "stale"
    assert env["redis"].get(state_keys_pure.latest_head("o/r", 1)).decode() == "c" * 40


def test_current_commit_runs_even_if_redis_was_overwritten(env) -> None:
    """반대 순서: 새 커밋의 작업이 먼저 꺼내졌는데 Redis에는 늦게 온 옛 커밋이 적혀 있다."""
    env["redis"].set(state_keys_pure.latest_head("o/r", 1), "0" * 40)
    use_runner(env, lambda *a, **k: PASSED)
    assert main.handle(_payload(), env["deps"]) == "passed"
    assert env["redis"].get(state_keys_pure.latest_head("o/r", 1)).decode() == HEAD


@pytest.mark.parametrize("final", [False, True])
def test_head_lookup_failure_is_environment_error(env, final: bool) -> None:
    env["gh"].head_error = "gh: 401"
    env["monkeypatch"].setattr(retry, "attempt", lambda: (4 if final else 1, final))
    job = PRJob.model_validate_json(_payload())
    env["redis"].set(state_keys_pure.job(job.dedup_key), "1")
    with pytest.raises(GitHubError):
        main.handle(_payload(), env["deps"])
    assert _states(env) == []
    assert (env["redis"].get(state_keys_pure.job(job.dedup_key)) is None) == final


@pytest.mark.parametrize(
    "error", [RuntimeError("버그"), JobTimeoutException("30분 초과")], ids=["bug", "job-timeout"]
)
def test_unexpected_error_is_recorded_and_retried(env, error: Exception) -> None:
    use_runner(env, lambda *a, **k: (_ for _ in ()).throw(error))
    env["monkeypatch"].setattr(retry, "attempt", lambda: (1, False))
    job = PRJob.model_validate_json(_payload())
    env["redis"].set(state_keys_pure.job(job.dedup_key), "1")
    with pytest.raises(type(error)):
        main.handle(_payload(), env["deps"])
    assert _states(env) == ["error"]  # running으로 남으면 동기화가 죽은 워커로 보고 계속 넣는다
    body = bodies(env["gh"])[-1]
    assert "파이프라인 오류로 대기 중" in body and "실패" not in body
    assert env["redis"].get(state_keys_pure.job(job.dedup_key)) is not None  # 재시도 동안 키 유지


def test_last_unexpected_error_releases_key(env) -> None:
    use_runner(env, lambda *a, **k: (_ for _ in ()).throw(RuntimeError("버그")))
    env["monkeypatch"].setattr(retry, "attempt", lambda: (4, True))
    job = PRJob.model_validate_json(_payload())
    env["redis"].set(state_keys_pure.job(job.dedup_key), "1")
    with pytest.raises(RuntimeError):
        main.handle(_payload(), env["deps"])
    assert "파이프라인 오류로 검사하지 못함" in bodies(env["gh"])[-1]
    assert env["redis"].get(state_keys_pure.job(job.dedup_key)) is None


def test_commit_that_goes_stale_during_tests_leaves_no_result(env) -> None:
    def run_then_new_commit(*args, **kwargs):
        env["redis"].set(state_keys_pure.latest_head("o/r", 1), "c" * 40)
        return PASSED

    use_runner(env, run_then_new_commit)
    assert main.handle(_payload(), env["deps"]) == "stale"
    assert not any("통과" in b for b in bodies(env["gh"]))  # 새 커밋의 작업이 코멘트를 고친다


def test_environment_error_shows_wait_and_raises_for_retry(env) -> None:
    def broken(*args, **kwargs):
        raise SandboxError("docker 중단")

    use_runner(env, broken)
    env["monkeypatch"].setattr(retry, "attempt", lambda: (1, False))
    job = PRJob.model_validate_json(_payload())
    env["redis"].set(state_keys_pure.job(job.dedup_key), "1")
    with pytest.raises(SandboxError):
        main.handle(_payload(), env["deps"])
    body = bodies(env["gh"])[-1]
    assert "환경 문제로 대기 중" in body and "실패" not in body
    assert env["redis"].get(state_keys_pure.job(job.dedup_key)) is not None  # 재시도 동안 키 유지


def test_last_environment_retry_says_not_checked_and_releases_key(env) -> None:
    use_runner(env, lambda *a, **k: (_ for _ in ()).throw(SandboxError("x")))
    env["monkeypatch"].setattr(retry, "attempt", lambda: (4, True))
    job = PRJob.model_validate_json(_payload())
    env["redis"].set(state_keys_pure.job(job.dedup_key), "1")
    with pytest.raises(SandboxError):
        main.handle(_payload(), env["deps"])
    assert "환경 문제로 검사하지 못함" in bodies(env["gh"])[-1]
    # 동기화가 30분 뒤 같은 커밋을 다시 넣을 수 있어야 한다
    assert env["redis"].get(state_keys_pure.job(job.dedup_key)) is None


def test_resolution_failure_is_unrunnable_at_once(env) -> None:
    def no_package(*args, **kwargs):
        raise DependencyInstallError("resolution", "key", "No matching distribution found for x")

    use_runner(env, no_package)
    assert main.handle(_payload(), env["deps"]) == "unrunnable"
    body = bodies(env["gh"])[-1]
    assert "실행 불가 (의존성을 설치하지 못함)" in body
    assert "**규칙:** 규칙을 찾지 못해" in body  # 규칙 경로는 설치와 따로 돈다


def test_tests_get_repo_memory_limit(env) -> None:
    seen = {}

    def runner(*args, **kwargs):
        seen.update(kwargs)
        return PASSED

    use_runner(env, runner)
    main.handle(_payload(), env["deps"])
    assert seen["memory_gb"] == BASE.memory_gb


def test_commit_that_goes_stale_during_rule_reading_stops(env) -> None:
    def newer_commit_arrives(*a, **k):
        env["redis"].set(state_keys_pure.latest_head("o/r", 1), "c" * 40)
        return NoRuleDocs()

    env["monkeypatch"].setattr(doc_rules, "read", newer_commit_arrives)
    use_runner(env, lambda *a, **k: pytest.fail("테스트까지 가면 안 된다"))
    assert main.handle(_payload(), env["deps"]) == "stale"


@pytest.mark.parametrize(("attempt", "unrunnable"), [(1, False), (2, True)])
def test_unknown_install_failure_uses_retry_count(env, attempt: int, unrunnable: bool) -> None:
    def build_failure(*args, **kwargs):
        raise DependencyInstallError("unknown", "key", "Failed building wheel")

    use_runner(env, build_failure)
    env["monkeypatch"].setattr(retry, "attempt", lambda: (attempt, False))
    if unrunnable:
        assert main.handle(_payload(), env["deps"]) == "unrunnable"
    else:
        with pytest.raises(DependencyInstallError):
            main.handle(_payload(), env["deps"])
        assert "환경 문제로 대기 중" in bodies(env["gh"])[-1]


def test_sync_job_result_says_so(env) -> None:
    use_runner(env, lambda *a, **k: PASSED)
    main.handle(_payload(source="sync"), env["deps"])
    assert "동기화로 처리됨 (웹훅 누락)" in bodies(env["gh"])[-1]


def _checks(*failing: str):
    """시작 전 확인의 결과. failing에 든 종류만 실패한다."""
    names = {"gh_login": "GitHub 로그인", "claude_login": "Claude 로그인", "docker": "Docker"}

    def run(redis) -> list[preflight.Check]:
        return [preflight.Check(n, k not in failing, "", k) for k, n in names.items()]

    return run


def _never(redis) -> list[preflight.Check]:
    raise AssertionError("알리지 않을 실패에서 시작 전 확인을 다시 돌림")


def test_only_used_up_environment_failures_notify_what_to_do(env, notifications) -> None:
    """6-4: 재시도를 모두 쓴 환경 오류만 시작 전 확인으로 다시 가려, 실패한 확인의 정한 한 줄을
    띄운다. 재시도가 남은 실패, 확인이 모두 통과하는 환경 오류(가리지 못함), 파이프라인 오류는
    알리지 않는다. 같은 종류는 다른 PR에서 다시 나도 30분에 한 번이다. 예외 메시지는 들어가지
    않는다."""
    env["gh"].head_error = "gh: 401 token=ghp_should_not_leave"

    def fail(final: bool, checks, pr: int = 1) -> None:
        env["monkeypatch"].setattr(retry, "attempt", lambda: (4 if final else 1, final))
        with pytest.raises(GitHubError):
            main.handle(_payload(pr_number=pr), env["deps"], checks=checks)

    fail(False, _never)
    fail(True, _checks())
    assert notifications == []
    fail(True, _checks("docker"))
    fail(True, _checks("docker"), pr=2)
    assert notifications == [TEXT["docker"]]
    env["gh"].head_error = None
    use_runner(env, lambda *a, **k: (_ for _ in ()).throw(RuntimeError("버그 ghp_x")))
    env["monkeypatch"].setattr(retry, "attempt", lambda: (4, True))
    with pytest.raises(RuntimeError):
        main.handle(_payload(), env["deps"], checks=_never)
    assert notifications == [TEXT["docker"]]
    assert not any("ghp_" in text or "401" in text for text in notifications)


def test_repeated_start_up_failures_notify_once_per_window(
    tmp_path: Path, monkeypatch, notifications
) -> None:
    """launchd가 시작 전 확인에서 멈춘 워커를 1분마다 다시 띄워도 같은 오류는 30분에 한 번만 뜬다.
    표시는 data_dir의 파일이라 프로세스를 넘어 남는다. 종류가 다르면 따로 뜬다."""
    settings = config.PipelineConfig(repos=["o/r"], data_dir=tmp_path)
    monkeypatch.setattr(main.config, "load_config", lambda: settings)
    monkeypatch.setattr(main.Redis, "from_url", lambda url: fakeredis.FakeStrictRedis())
    monkeypatch.setattr(main.preflight, "run_checks", _checks("claude_login", "docker"))
    assert [main.main() for _ in range(5)] == [1] * 5
    assert notifications == [TEXT["claude_login"], TEXT["docker"]]
