"""테스트 실행 결과와 판정에 쓰는 상태.

결과를 읽는 쪽(지금은 Python 프로필)이 테스트 단계의 결과를 `TestVerdict`로 정해 넘긴다. 도구마다
다른 종료 코드의 뜻은 여기에 두지 않는다.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

KILLED_EXIT: Final = 137  # SIGKILL. 컨테이너 메모리 한도를 넘어 OOM으로 종료됨

# 환경 오류(Docker·설치·컨테이너 문제)는 상태가 아니라 SandboxError로 올라간다.
# unrunnable은 컨테이너 안에서 테스트가 돌았지만 PR 코드 때문에 결과를 못 낸 경우다.
# no_env는 저장소에 돌릴 환경이 없어 아무것도 돌리지 않은 경우다 (7.7). 까닭은 TestRun.unplanned다.
InputsSource = Literal["base", "head"]
RunStatus = Literal[
    "passed", "failed", "build_failed", "no_tests", "timeout", "cancelled", "unrunnable", "no_env"
]  # fmt: skip
TestVerdict = Literal["passed", "failed", "no_tests", "unrunnable"]

# 이 모양 전의 체크포인트에 남은 "Python 프로필이 돌릴 것이 없음"의 까닭 (파일 끝의 복원만 쓴다)
NO_PROFILE_WHY: Final = (
    "이 저장소에는 Python 파일이 없고, pr-proof가 빌드·테스트·정적 검사를 돌릴 수 있는 언어는 "
    "지금 Python뿐임"
)


# 체크포인트에서 되살릴 수 있게 graph.py가 등록하는 타입
STATE_TYPES: Final = (
    ("app.worker.testing.test_run_pure", "StepRun"),
    ("app.worker.testing.test_run_pure", "TestReport"),
    ("app.worker.testing.test_run_pure", "TestRun"),
    ("app.worker.testing.test_run_pure", "EnvUse"),
    ("app.worker.testing.test_run_pure", "PytestRun"),  # 이 모양 전의 체크포인트 (파일 끝)
    ("app.worker.testing.test_run_pure", "ReportSummary"),
)


@dataclass(frozen=True)
class StepRun:
    """샌드박스에서 돌린 명령 하나. output은 출력의 끝 부분이다."""

    name: str
    exit_code: int | None
    timed_out: bool = False
    cancelled: bool = False
    oom_killed: bool = False  # 컨테이너가 메모리 한도로 죽음 (docker inspect OOMKilled)
    output: str = field(default="", repr=False)
    seconds: float = 0.0  # 걸린 시간. 환경 처방의 실행만 잰다


@dataclass(frozen=True)
class TestReport:
    """테스트 결과 파일의 요약. failed_tests는 실패와 오류의 이름이다."""

    __test__ = False  # 이름이 Test로 시작해도 pytest가 테스트 클래스로 모으지 않게

    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    total: int = 0
    failed_tests: tuple[str, ...] = ()


@dataclass(frozen=True)
class EnvUse:
    """환경 처방으로 돌렸을 때의 환경. 처방 본문은 env_plans에서 plan_id로 다시 읽는다.

    layers는 (층 이름, 다시 씀)이다. inputs는 의존성 층을 구운 입력이 어느 커밋의 것인지이고,
    changed_inputs는 PR이 바꾼 deps.inputs다. inputs가 base면 그 파일은 쓰지 않았다.
    """

    plan_id: int
    layers: tuple[tuple[str, bool], ...] = ()
    inputs: InputsSource = "base"
    changed_inputs: tuple[str, ...] = ()


@dataclass(frozen=True)
class TestRun:
    """빌드 단계들과 테스트 단계를 한 번 돌린 결과. 산출물 경로는 worktree의 .pipeline-out/ 아래다.

    verdict와 unrunnable_why는 테스트 단계의 결과를 읽은 쪽이 정한다. unplanned가 있으면 돌릴
    환경이 없어 아무것도 돌리지 않은 것이고, 나머지 값은 비어 있다.
    """

    __test__ = False

    build: tuple[StepRun, ...] = ()
    test: StepRun | None = None
    verdict: TestVerdict | None = None
    unrunnable_why: str = ""
    report: TestReport | None = None  # 결과 파일이 없거나 깨졌으면 None
    report_path: Path | None = None
    coverage_xml: Path | None = None
    # 처음 실행에서 실패했다가 한 번 더 돌려 통과한 테스트 (DESIGN.md F8 flaky)
    flaky: tuple[str, ...] = ()
    unplanned: str = ""
    env: EnvUse | None = None  # 환경 처방으로 돌렸으면 그 환경. Python 프로필이면 None

    @property
    def _steps(self) -> tuple[StepRun, ...]:
        # 체크포인트에서 되살리면 튜플이 리스트로 돌아온다
        return (*self.build, *((self.test,) if self.test is not None else ()))

    @property
    def failed_build(self) -> StepRun | None:
        """처음 실패한 빌드 단계. 빌드 단계는 처음 실패에서 멈춘다."""
        return next((s for s in self.build if s.exit_code != 0), None)

    @property
    def cancelled(self) -> bool:
        return any(s.cancelled for s in self._steps)

    @property
    def output(self) -> str:
        """상태를 설명하는 출력: 빌드가 실패했으면 그 단계의 것, 아니면 테스트 단계의 것."""
        step = self.failed_build or self.test
        return step.output if step is not None else ""

    @property
    def passed_on_rerun(self) -> bool:
        """실패한 테스트가 모두 재실행에서 통과했다. 수집 오류·테스트 오류가 있으면 아니다."""
        r = self.report
        if r is None or not self.flaky or r.errors:
            return False
        return set(r.failed_tests) <= set(self.flaky)

    @property
    def passed_count(self) -> int:
        """재실행에서 통과한 테스트까지 센 통과 수."""
        if self.report is None:
            return 0
        return self.report.passed + (len(self.flaky) if self.passed_on_rerun else 0)

    @property
    def ok(self) -> bool:
        return self.status == "passed"

    @property
    def status(self) -> RunStatus:
        # 취소·시간 초과를 먼저 본다. 이때 파이프라인이 컨테이너를 지워 137이 나올 수 있다
        if self.cancelled:
            return "cancelled"
        if self.unplanned:
            return "no_env"
        if any(s.timed_out for s in self._steps):
            return "timeout"
        if self.failed_build is not None:
            return "build_failed"
        if self.verdict == "failed" and self.passed_on_rerun:
            return "passed"  # 불안정한 테스트가 있다 (flaky). 코드 수정을 확인하는 데는 쓰지 않는다
        return self.verdict or "unrunnable"

    @property
    def unrunnable_reason(self) -> str | None:
        """실행 불가일 때 PR 코멘트에 쓸 이유."""
        if self.status != "unrunnable":
            return None
        if self.memory_exceeded:
            return "메모리 한도를 넘어 종료됨"
        return self.unrunnable_why or "테스트 결과를 읽지 못함"

    @property
    def memory_exceeded(self) -> bool:
        """메모리 한도 초과로 종료됨. 취소·시간 초과가 아닐 때만 137을 메모리 초과로 본다."""
        t = self.test
        return (
            self.status == "unrunnable"
            and t is not None
            and (t.oom_killed or t.exit_code == KILLED_EXIT)
        )

    @property
    def allows_mechanical_fix(self) -> bool:
        """동작을 바꾸지 않는 기계적 수정을 할 수 있는지 (4.6 표)."""
        return self.status in ("passed", "no_tests")

    @property
    def allows_code_fix(self) -> bool:
        """모델 수정처럼 코드를 바꾸는 수정을 할 수 있는지. 위반 줄 커버리지는 따로 본다.

        불안정한 테스트(flaky)가 있으면 수정 뒤 결과를 믿을 수 없으므로 막는다.
        """
        return self.status == "passed" and not self.flaky


# --- 이 모양 전의 체크포인트 (2026-11-29 전) ---
# langgraph는 체크포인트의 값을 (모듈, 클래스 이름)으로 찾아 저장한 필드로 다시 만든다. 그 전의
# 이름 PytestRun·ReportSummary로 저장된 값을 새 모양으로 바꾼다. 옛 값에는 판정이 없어 그때의
# pytest 종료 코드 표로 정한다. 저장된 형식의 해석이라 Python 프로필이 바뀌어도 바꾸지 않는다.

_OLD_UNRUNNABLE_WHY: Final[Mapping[int, str]] = {3: "pytest 내부 오류", 4: "pytest 설정·인자 오류"}


def _old_verdict(exit_code: int | None, report: TestReport | None) -> tuple[TestVerdict, str]:
    if exit_code == 5:
        return "no_tests", ""
    if report is not None and exit_code == 0:
        return "passed", ""
    if report is not None and exit_code in (1, 2):
        return "failed", ""
    if exit_code in _OLD_UNRUNNABLE_WHY:
        return "unrunnable", _OLD_UNRUNNABLE_WHY[exit_code]
    if report is None:
        return "unrunnable", "결과 파일 없음 (conftest 등 수집 전 오류)"
    return "unrunnable", f"pytest 종료 코드 {exit_code}"


def _restore_pytest_run(
    *,
    exit_code: int | None,
    summary: TestReport | None,
    report_path: Path | None = None,
    coverage_xml: Path | None = None,
    timed_out: bool = False,
    cancelled: bool = False,
    oom_killed: bool = False,
    output: str = "",
    flaky: tuple[str, ...] = (),
    no_profile: bool = False,
) -> TestRun:
    if no_profile:
        return TestRun(unplanned=NO_PROFILE_WHY)
    verdict, why = _old_verdict(exit_code, summary)
    step = StepRun("pytest", exit_code, timed_out, cancelled, oom_killed, output)
    return TestRun(
        test=step,
        verdict=verdict,
        unrunnable_why=why,
        report=summary,
        report_path=report_path,
        coverage_xml=coverage_xml,
        flaky=flaky,
    )


PytestRun: Final = _restore_pytest_run
ReportSummary: Final = TestReport  # 필드가 같다
