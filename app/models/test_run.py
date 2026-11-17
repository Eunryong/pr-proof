"""테스트 실행 결과와 판정에 쓰는 상태."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

# pytest 종료 코드: 0 통과, 1 실패, 2 중단(수집 오류 포함), 3 내부 오류, 4 사용법 오류, 5 없음
PYTEST_FAILED: Final = (1, 2)
PYTEST_INTERNAL_ERROR: Final = 3
PYTEST_USAGE_ERROR: Final = 4
PYTEST_NO_TESTS: Final = 5
KILLED_EXIT: Final = 137  # SIGKILL. 컨테이너 메모리 한도를 넘어 OOM으로 종료됨

# 환경 오류(Docker·설치·컨테이너 문제)는 상태가 아니라 SandboxError로 올라간다.
# unrunnable은 컨테이너 안에서 pytest가 돌았지만 PR 코드 때문에 결과를 못 낸 경우다.
RunStatus = Literal["passed", "failed", "no_tests", "timeout", "cancelled", "unrunnable"]


@dataclass(frozen=True)
class ReportSummary:
    """pytest-json-report 결과 요약."""

    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    total: int = 0
    failed_tests: tuple[str, ...] = ()

    @classmethod
    def from_report(cls, data: dict) -> "ReportSummary":
        summary = data.get("summary", {})
        failed_ids = [
            t["nodeid"] for t in data.get("tests", []) if t.get("outcome") in ("failed", "error")
        ]
        # 수집 단계 오류(import 실패 등)는 tests가 아니라 collectors에 남는다
        collect_errors = [
            c["nodeid"] for c in data.get("collectors", []) if c.get("outcome") == "failed"
        ]
        return cls(
            passed=summary.get("passed", 0),
            failed=summary.get("failed", 0),
            errors=summary.get("error", 0) + len(collect_errors),
            skipped=summary.get("skipped", 0),
            total=summary.get("total", 0),
            failed_tests=tuple(failed_ids + collect_errors),
        )


@dataclass(frozen=True)
class PytestRun:
    """샌드박스에서 pytest를 한 번 실행한 결과. 산출물 경로는 worktree의 .pipeline-out/ 아래다."""

    exit_code: int | None
    summary: ReportSummary | None  # report.json이 없거나 깨졌으면 None
    report_path: Path | None = None
    coverage_xml: Path | None = None
    timed_out: bool = False
    cancelled: bool = False
    output: str = field(default="", repr=False)

    @property
    def ok(self) -> bool:
        return self.status == "passed"

    @property
    def status(self) -> RunStatus:
        if self.cancelled:
            return "cancelled"
        if self.timed_out:
            return "timeout"
        if self.exit_code == 0 and self.summary is not None:
            return "passed"
        if self.exit_code in PYTEST_FAILED and self.summary is not None:
            return "failed"
        if self.exit_code == PYTEST_NO_TESTS:
            return "no_tests"
        return "unrunnable"

    @property
    def unrunnable_reason(self) -> str | None:
        """실행 불가일 때 PR 코멘트에 쓸 이유."""
        if self.status != "unrunnable":
            return None
        if self.exit_code == KILLED_EXIT:
            return "메모리 한도를 넘어 종료됨"
        if self.exit_code == PYTEST_INTERNAL_ERROR:
            return "pytest 내부 오류"
        if self.exit_code == PYTEST_USAGE_ERROR:
            return "pytest 설정·인자 오류"
        if self.summary is None:
            return "결과 파일 없음 (conftest 등 수집 전 오류)"
        return f"pytest 종료 코드 {self.exit_code}"

    @property
    def allows_lint_fix(self) -> bool:
        """동작을 바꾸지 않는 컨벤션 수정을 제안할 수 있는지 (DESIGN.md F8)."""
        return self.status in ("passed", "no_tests")

    @property
    def allows_code_fix(self) -> bool:
        """정적변수·구조처럼 코드를 바꾸는 수정을 제안할 수 있는지. 위반 줄 커버리지는 따로 본다.

        flaky 재실행이 들어오면 flaky가 없다는 조건을 더한다.
        """
        return self.status == "passed"
