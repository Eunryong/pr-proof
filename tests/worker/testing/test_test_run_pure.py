from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import pytest
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.worker import flow_state
from app.worker.testing.test_run_pure import NO_PROFILE_WHY, StepRun, TestReport, TestRun

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


# 이 모양 전의 체크포인트에 저장된 값: 그때의 클래스 이름과 필드 그대로
@dataclass(frozen=True)
class _OldSummary:
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    total: int = 0
    failed_tests: tuple[str, ...] = ()


@dataclass(frozen=True)
class _OldRun:
    exit_code: int | None
    summary: _OldSummary | None
    report_path: Path | None = None
    coverage_xml: Path | None = None
    timed_out: bool = False
    cancelled: bool = False
    oom_killed: bool = False
    output: str = field(default="", repr=False)
    flaky: tuple[str, ...] = ()
    no_profile: bool = False


for _cls, _name in ((_OldSummary, "ReportSummary"), (_OldRun, "PytestRun")):
    _cls.__module__, _cls.__name__ = "app.worker.testing.test_run_pure", _name


def _restore(value: object) -> object:
    serde = JsonPlusSerializer(allowed_msgpack_modules=list(flow_state._STATE_TYPES))
    return serde.loads_typed(serde.dumps_typed({"test_run": value}))["test_run"]


@pytest.mark.parametrize(
    ("old", "status", "passed", "reason"),
    [
        (_OldRun(0, _OldSummary(passed=9, total=9), report_path=Path("/w/r.json")), "passed", 9,
         None),
        (_OldRun(1, _OldSummary(passed=8, failed=1, total=9, failed_tests=("t::a",)),
                 flaky=("t::a",)), "passed", 9, None),
        (_OldRun(1, _OldSummary(passed=8, failed=1, total=9)), "failed", 8, None),
        (_OldRun(5, _OldSummary()), "no_tests", 0, None),
        (_OldRun(4, None, output="usage"), "unrunnable", 0, "pytest 설정·인자 오류"),
        (_OldRun(137, None), "unrunnable", 0, "메모리 한도를 넘어 종료됨"),
        (_OldRun(None, None, cancelled=True), "cancelled", 0, None),
        (_OldRun(None, None, no_profile=True), "no_env", 0, None),
    ],
    ids=["passed", "flaky", "failed", "no_tests", "usage", "oom", "cancelled", "no_profile"],
)  # fmt: skip
def test_a_checkpoint_from_before_this_shape_restores_as_a_test_run(
    old: _OldRun, status: str, passed: int, reason: str | None
) -> None:
    run = _restore(old)
    assert isinstance(run, TestRun)
    assert (run.status, run.passed_count, run.unrunnable_reason) == (status, passed, reason)
    assert run.report_path == old.report_path
    if old.no_profile:
        assert run.unplanned == NO_PROFILE_WHY


def test_a_new_run_survives_the_checkpoint() -> None:
    run = TestRun(
        build=(StepRun("cmake", 0),), test=StepRun("ctest", 8, output="1 failed"), verdict="failed",
        report=TestReport(passed=16, failed=1, total=17, failed_tests=("a.b",)),
    )  # fmt: skip
    restored = _restore(run)
    assert isinstance(restored, TestRun)
    assert (restored.status, restored.output, restored.passed_count) == ("failed", "1 failed", 16)
