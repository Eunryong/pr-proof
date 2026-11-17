"""테스트 실행 결과와 판정에 쓰는 상태."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

# pytest 종료 코드: 0 통과, 1 실패, 2 중단(수집 오류 포함), 5 테스트 없음
PYTEST_FAILED: Final = (1, 2)
PYTEST_NO_TESTS: Final = 5

RunStatus = Literal["passed", "failed", "no_tests", "timeout", "cancelled", "error"]


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
        return "error"  # pytest 내부 오류, 사용법 오류, 결과 파일 없음
