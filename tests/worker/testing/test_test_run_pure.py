from typing import Final

import pytest

from app.worker.testing.test_run_pure import StepRun, TestReport, TestRun

SUMMARY: Final = TestReport(passed=1, total=1)
FLAKY_SUMMARY: Final = TestReport(passed=8, failed=1, total=9, failed_tests=("t::a",))
BUILD_OK: Final = StepRun("cmake", 0)
BUILD_BROKEN: Final = StepRun("cmake", 2, output="error: no member named 'x'")
NO_ENV_WHY: Final = "C++ 환경이 없음"


def ran(verdict: str | None, report: TestReport | None = SUMMARY, **step) -> TestRun:
    return TestRun(test=StepRun("test", step.pop("exit_code", 0), **step), verdict=verdict,
                   report=report)  # fmt: skip


@pytest.mark.parametrize(
    ("run", "status"),
    [
        (ran("passed"), "passed"),
        (ran("failed"), "failed"),
        (ran("no_tests", TestReport()), "no_tests"),
        (ran("unrunnable", None), "unrunnable"),
        (ran(None, None), "unrunnable"),
        (TestRun(), "unrunnable"),
        (ran("passed", timed_out=True), "timeout"),
        (ran("passed", cancelled=True), "cancelled"),
        (TestRun(build=(BUILD_OK,), test=StepRun("test", 0), verdict="passed"), "passed"),
        (TestRun(build=(BUILD_OK, BUILD_BROKEN)), "build_failed"),
        (TestRun(build=(BUILD_BROKEN,), test=StepRun("test", 0), verdict="passed"), "build_failed"),
        (TestRun(build=(StepRun("cmake", None, timed_out=True),)), "timeout"),
        (TestRun(build=(StepRun("cmake", 137, cancelled=True),)), "cancelled"),
        (TestRun(unplanned=NO_ENV_WHY), "no_env"),
    ],
    ids=[
        "passed", "failed", "no_tests", "unrunnable", "no-verdict", "empty", "timeout",
        "cancelled", "build-then-passed", "build-failed", "build-failed-beats-verdict",
        "build-timeout", "build-cancelled", "no_env",
    ],
)  # fmt: skip
def test_status(run: TestRun, status: str) -> None:
    assert run.status == status
    assert run.ok is (status == "passed")


@pytest.mark.parametrize(
    ("run", "status"),
    [
        (TestRun(build=(BUILD_BROKEN,), test=StepRun("test", None, cancelled=True)), "cancelled"),
        (TestRun(unplanned=NO_ENV_WHY, test=StepRun("test", None, cancelled=True)), "cancelled"),
        (TestRun(unplanned=NO_ENV_WHY, test=StepRun("test", None, timed_out=True)), "no_env"),
        (TestRun(build=(BUILD_BROKEN, StepRun("make", None, timed_out=True))), "timeout"),
    ],
    ids=["cancel-beats-build", "cancel-beats-no_env", "no_env-beats-timeout",
         "timeout-beats-build"],
)  # fmt: skip
def test_status_order(run: TestRun, status: str) -> None:
    """취소 → 환경 없음 → 시간 초과 → 빌드 실패 → 테스트 단계의 판정 순으로 본다."""
    assert run.status == status


def test_no_env_carries_its_reason_and_allows_no_fix() -> None:
    """돌릴 환경이 없으면 돌리지 않은 것이다. 고친 것을 확인할 수 없으므로 어느 수정도 허용하지
    않고, PR 코드 때문에 못 돌린 것(unrunnable)과 섞이지 않는다."""
    run = TestRun(unplanned=NO_ENV_WHY)
    assert run.unplanned == NO_ENV_WHY
    assert not run.allows_mechanical_fix and not run.allows_code_fix
    assert run.unrunnable_reason is None and not run.memory_exceeded


def test_build_failed_allows_no_fix_and_shows_the_build_output() -> None:
    run = TestRun(build=(BUILD_OK, BUILD_BROKEN), test=StepRun("test", 0, output="ok"))
    assert not run.allows_mechanical_fix and not run.allows_code_fix
    assert run.output == "error: no member named 'x'"
    assert run.unrunnable_reason is None


@pytest.mark.parametrize(
    ("run", "reason"),
    [
        (ran("unrunnable", None, exit_code=137), "메모리 한도를 넘어 종료됨"),
        (TestRun(test=StepRun("test", 1, oom_killed=True), verdict="unrunnable",
                 unrunnable_why="결과 없음"), "메모리 한도를 넘어 종료됨"),
        (TestRun(test=StepRun("test", 3), verdict="unrunnable", unrunnable_why="내부 오류"),
         "내부 오류"),
        (TestRun(), "테스트 결과를 읽지 못함"),
        (ran("passed"), None),
    ],
    ids=["exit-137", "oom-flag", "reader-why", "no-why", "not-unrunnable"],
)  # fmt: skip
def test_unrunnable_reason(run: TestRun, reason: str | None) -> None:
    assert run.unrunnable_reason == reason


@pytest.mark.parametrize(
    ("run", "lint", "code"),
    [
        (ran("passed"), True, True),
        (ran("no_tests", TestReport()), True, False),  # 테스트로 확인할 수 없음
        (ran("failed"), False, False),
        (ran("passed", timed_out=True), False, False),
        (ran("unrunnable", None), False, False),
        (ran("passed", cancelled=True), False, False),
        (TestRun(test=StepRun("test", 1), verdict="failed", report=FLAKY_SUMMARY,
                 flaky=("t::a",)), True, False),
        (TestRun(build=(BUILD_BROKEN,)), False, False),
    ],
    ids=["passed", "no_tests", "failed", "timeout", "unrunnable", "cancelled", "flaky",
         "build_failed"],
)  # fmt: skip
def test_fix_permissions_follow_safety_net(run: TestRun, lint: bool, code: bool) -> None:
    assert (run.allows_mechanical_fix, run.allows_code_fix) == (lint, code)


@pytest.mark.parametrize(
    ("run", "exceeded"),
    [
        (ran("unrunnable", None, exit_code=137), True),
        (ran("unrunnable", None, exit_code=137, cancelled=True), False),  # 파이프라인이 지움
        (ran("unrunnable", None, exit_code=137, timed_out=True), False),
        (ran("unrunnable", None, exit_code=4), False),
        (TestRun(build=(StepRun("cmake", 137),)), False),  # 빌드 실패로 보인다
    ],
    ids=["exit-137", "cancelled", "timed_out", "usage-error", "build-killed"],
)  # fmt: skip
def test_memory_exceeded_checks_cancel_and_timeout_first(run: TestRun, exceeded: bool) -> None:
    assert run.memory_exceeded is exceeded


def test_flaky_status_and_counts() -> None:
    run = TestRun(test=StepRun("test", 1), verdict="failed", report=FLAKY_SUMMARY, flaky=("t::a",))
    assert run.status == "passed" and run.passed_count == 9


def test_partly_flaky_is_still_failed() -> None:
    report = TestReport(passed=7, failed=2, total=9, failed_tests=("t::a", "t::b"))
    run = TestRun(test=StepRun("test", 1), verdict="failed", report=report, flaky=("t::a",))
    assert run.status == "failed" and run.passed_count == 7


def test_errors_are_never_flaky() -> None:
    report = TestReport(passed=8, errors=1, total=9, failed_tests=("t::a",))
    run = TestRun(test=StepRun("test", 1), verdict="failed", report=report, flaky=("t::a",))
    assert run.status == "failed"
