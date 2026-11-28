"""테스트 실행 결과와 판정에 쓰는 상태.

결과를 읽는 쪽(지금은 Python 프로필)이 테스트 단계의 결과를 `TestVerdict`로 정해 넘긴다. 도구마다
다른 종료 코드의 뜻은 여기에 두지 않는다.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

KILLED_EXIT: Final = 137  # SIGKILL. 컨테이너 메모리 한도를 넘어 OOM으로 종료됨

# 환경 오류(Docker·설치·컨테이너 문제)는 상태가 아니라 SandboxError로 올라간다.
# unrunnable은 컨테이너 안에서 테스트가 돌았지만 PR 코드 때문에 결과를 못 낸 경우다.
# no_env는 저장소에 돌릴 환경이 없어 아무것도 돌리지 않은 경우다 (7.7). 까닭은 TestRun.unplanned다.
RunStatus = Literal[
    "passed", "failed", "build_failed", "no_tests", "timeout", "cancelled", "unrunnable", "no_env"
]  # fmt: skip
TestVerdict = Literal["passed", "failed", "no_tests", "unrunnable"]

# Python 프로필이 돌릴 것이 없을 때의 까닭 (graph.run_pr_tests)
NO_PROFILE_WHY: Final = (
    "이 저장소에는 Python 파일이 없고, pr-proof가 빌드·테스트·정적 검사를 돌릴 수 있는 언어는 "
    "지금 Python뿐임"
)


# 체크포인트에서 되살릴 수 있게 graph.py가 등록하는 타입
STATE_TYPES: Final = (
    ("app.worker.testing.test_run_pure", "StepRun"),
    ("app.worker.testing.test_run_pure", "TestReport"),
    ("app.worker.testing.test_run_pure", "TestRun"),
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

    @property
    def _steps(self) -> tuple[StepRun, ...]:
        # 체크포인트에서 되살리면 튜플이 리스트로 돌아온다
        return (*self.build, *((self.test,) if self.test is not None else ()))

    @property
    def _failed_build(self) -> StepRun | None:
        return next((s for s in self.build if s.exit_code != 0), None)

    @property
    def cancelled(self) -> bool:
        return any(s.cancelled for s in self._steps)

    @property
    def output(self) -> str:
        """상태를 설명하는 출력: 빌드가 실패했으면 그 단계의 것, 아니면 테스트 단계의 것."""
        step = self._failed_build or self.test
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
        if self._failed_build is not None:
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
