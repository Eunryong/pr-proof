"""처방의 테스트 단계 결과를 판정한다. 결과 파일 읽기는 호출하는 쪽이 한다."""

from typing import assert_never

from app.worker.env.recipe_pure import TestReportKind
from app.worker.testing.reports_pure import read_junit, summarize
from app.worker.testing.test_run_pure import TestReport, TestVerdict


def judge_test(
    kind: TestReportKind, exit_code: int | None, junit: bytes | None, report_path: str
) -> tuple[TestVerdict, str, TestReport | None]:
    """테스트 단계의 판정, 실행 불가의 까닭, 결과 요약.

    junit: 결과 파일이 없거나 읽히지 않으면 실행 불가(까닭에 종료 코드), 테스트가 0개면 no_tests,
    실패·오류가 하나라도 있으면 failed, 모두 통과하고 0으로 끝나면 passed다. 모두 통과했는데 0이
    아닌 값으로 끝나면 결과를 믿을 수 없어 실행 불가다. exit: 종료 코드만 본다.
    """
    match kind:
        case "exit":
            return ("passed" if exit_code == 0 else "failed"), "", None
        case "junit":
            cases = None if junit is None else read_junit(junit)
            if cases is None:
                return (
                    "unrunnable",
                    f"결과 파일 {report_path}을 읽지 못함 (종료 코드 {exit_code})",
                    None,
                )
            report = summarize(cases)
            if not cases:
                return "no_tests", "", report
            if report.failed or report.errors:
                return "failed", "", report
            if exit_code == 0:
                return "passed", "", report
            why = f"결과 파일에는 실패가 없는데 테스트 명령이 종료 코드 {exit_code}로 끝남"
            return "unrunnable", why, report
        case _:
            assert_never(kind)
