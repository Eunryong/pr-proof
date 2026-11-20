"""worker/main.py·update.py: update 작업이 체크아웃부터 코멘트까지 가는 흐름. Docker·GitHub 없이."""

import pytest
from rq.timeouts import JobTimeoutException
from sqlmodel import Session, select

from app import config
from app.models import state_keys
from app.models.db import Run
from app.models.doc_rules import NoRuleDocs
from app.models.job import PRJob
from app.models.ops_alert import TEXT
from app.notify import slack
from app.sandbox.docker_cli import SandboxError
from app.worker import doc_rules, main, retry, update
from app.worker.github import GitHubError
from app.worker.lang.python import DependencyInstallError

from ..notify.test_slack import Endpoint, endpoint  # noqa: F401
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


def test_closed_pr_is_not_checked(env) -> None:
    """닫히기 전에 등록된 작업이 닫힌 뒤에 실행된다 (워커가 죽어 RQ가 나중에 다시 실행한 경우)."""
    env["gh"].state = "closed"
    use_runner(env, lambda *a, **k: pytest.fail("닫힌 PR을 검사하면 안 된다"))
    assert main.handle(_payload(), env["deps"]) == "closed"
    assert env["gh"].kinds() == []  # 코멘트도 제안 PR도 건드리지 않는다
    assert _states(env) == []
    # 닫힘 표시를 head로 덮지 않는다. 덮으면 돌고 있던 작업이 멈추지 않는다
    assert env["redis"].get(state_keys.latest_head("o/r", 1)).decode() == update.CLOSED_HEAD


def test_late_webhook_for_old_commit_restores_latest_head(env) -> None:
    """새 커밋 c의 웹훅 뒤에 옛 커밋 a의 웹훅이 늦게 와서 Redis가 a로 덮였다."""
    env["gh"].head = "c" * 40
    env["redis"].set(state_keys.latest_head("o/r", 1), HEAD)
    use_runner(env, lambda *a, **k: pytest.fail("옛 커밋을 검사하면 안 된다"))
    assert main.handle(_payload(), env["deps"]) == "stale"
    assert env["redis"].get(state_keys.latest_head("o/r", 1)).decode() == "c" * 40


def test_current_commit_runs_even_if_redis_was_overwritten(env) -> None:
    """반대 순서: 새 커밋의 작업이 먼저 꺼내졌는데 Redis에는 늦게 온 옛 커밋이 적혀 있다."""
    env["redis"].set(state_keys.latest_head("o/r", 1), "0" * 40)
    use_runner(env, lambda *a, **k: PASSED)
    assert main.handle(_payload(), env["deps"]) == "passed"
    assert env["redis"].get(state_keys.latest_head("o/r", 1)).decode() == HEAD


@pytest.mark.parametrize("final", [False, True])
def test_head_lookup_failure_is_environment_error(env, final: bool) -> None:
    env["gh"].head_error = "gh: 401"
    env["monkeypatch"].setattr(retry, "attempt", lambda: (4 if final else 1, final))
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
    use_runner(env, lambda *a, **k: (_ for _ in ()).throw(error))
    env["monkeypatch"].setattr(retry, "attempt", lambda: (1, False))
    job = PRJob.model_validate_json(_payload())
    env["redis"].set(state_keys.job(job.dedup_key), "1")
    with pytest.raises(type(error)):
        main.handle(_payload(), env["deps"])
    assert _states(env) == ["error"]  # running으로 남으면 동기화가 죽은 워커로 보고 계속 넣는다
    body = bodies(env["gh"])[-1]
    assert "파이프라인 오류로 대기 중" in body and "실패" not in body
    assert env["redis"].get(state_keys.job(job.dedup_key)) is not None  # 재시도 동안 키 유지


def test_last_unexpected_error_releases_key(env) -> None:
    use_runner(env, lambda *a, **k: (_ for _ in ()).throw(RuntimeError("버그")))
    env["monkeypatch"].setattr(retry, "attempt", lambda: (4, True))
    job = PRJob.model_validate_json(_payload())
    env["redis"].set(state_keys.job(job.dedup_key), "1")
    with pytest.raises(RuntimeError):
        main.handle(_payload(), env["deps"])
    assert "파이프라인 오류로 검사하지 못함" in bodies(env["gh"])[-1]
    assert env["redis"].get(state_keys.job(job.dedup_key)) is None


def test_commit_that_goes_stale_during_tests_leaves_no_result(env) -> None:
    def run_then_new_commit(*args, **kwargs):
        env["redis"].set(state_keys.latest_head("o/r", 1), "c" * 40)
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
    env["redis"].set(state_keys.job(job.dedup_key), "1")
    with pytest.raises(SandboxError):
        main.handle(_payload(), env["deps"])
    body = bodies(env["gh"])[-1]
    assert "환경 문제로 대기 중" in body and "실패" not in body
    assert env["redis"].get(state_keys.job(job.dedup_key)) is not None  # 재시도 동안 키 유지


def test_last_environment_retry_says_not_checked_and_releases_key(env) -> None:
    use_runner(env, lambda *a, **k: (_ for _ in ()).throw(SandboxError("x")))
    env["monkeypatch"].setattr(retry, "attempt", lambda: (4, True))
    job = PRJob.model_validate_json(_payload())
    env["redis"].set(state_keys.job(job.dedup_key), "1")
    with pytest.raises(SandboxError):
        main.handle(_payload(), env["deps"])
    assert "환경 문제로 검사하지 못함" in bodies(env["gh"])[-1]
    # 동기화가 30분 뒤 같은 커밋을 다시 넣을 수 있어야 한다
    assert env["redis"].get(state_keys.job(job.dedup_key)) is None


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
        env["redis"].set(state_keys.latest_head("o/r", 1), "c" * 40)
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


def test_last_failures_reach_slack_once_per_window(env, endpoint: Endpoint) -> None:  # noqa: F811
    """6-4: 마지막 시도의 운영 오류가 Slack(로컬 https 가짜 엔드포인트)에 온다. 같은 저장소의 같은
    오류는 다른 PR에서 다시 나도 30분 안에는 한 번만 온다. 앞 시도의 실패와 다른 종류는 따로 본다.
    메시지에는 오류의 종류, 저장소#번호, 정한 글만 있다."""
    folder = config.config_dir()  # worker/conftest.py가 정한 빈 설정 폴더
    folder.mkdir(parents=True, exist_ok=True)
    (folder / slack.SECRET_NAME).write_text(endpoint.url)
    (folder / slack.SECRET_NAME).chmod(0o600)
    env["monkeypatch"].setenv("SSL_CERT_FILE", str(endpoint.cert))
    env["gh"].head_error = "gh: 401 token=ghp_should_not_leave"

    def fail(final: bool, pr: int) -> None:
        env["monkeypatch"].setattr(retry, "attempt", lambda: (4 if final else 1, final))
        with pytest.raises(GitHubError):
            main.handle(_payload(pr_number=pr), env["deps"])

    fail(False, 1)
    assert endpoint.bodies == []  # 재시도가 남은 실패는 알리지 않는다
    fail(True, 1)
    fail(True, 2)
    fail(True, 1)
    env["gh"].head_error = None
    use_runner(env, lambda *a, **k: (_ for _ in ()).throw(RuntimeError("버그 ghp_x")))
    env["monkeypatch"].setattr(retry, "attempt", lambda: (4, True))
    with pytest.raises(RuntimeError):
        main.handle(_payload(), env["deps"])
    texts = [b["text"] for b in endpoint.bodies]
    assert texts == [
        "pr-proof 운영 알림 · env_error_final · o/r#1 · " + TEXT["env_error_final"],
        "pr-proof 운영 알림 · pipeline_error · o/r#1 · " + TEXT["pipeline_error"],
    ]
    assert not any("ghp_" in text or "401" in text or "버그" in text for text in texts)
