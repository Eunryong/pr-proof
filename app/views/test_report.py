"""테스트 실행 결과를 PR 코멘트용 마크다운으로 바꾼다. 판정을 새로 내지 않고 모델 값만 보여준다."""

from collections.abc import Mapping
from typing import Final

from app.models.test_run import PytestRun, RunStatus

MAX_LISTED_FAILURES: Final = 10

_HEADLINES: Final[Mapping[RunStatus, str]] = {
    "passed": "테스트 통과",
    "failed": "❌ 테스트 실패",
    "no_tests": "⚠️ 실행된 테스트 없음",
    "timeout": "❌ 시간 초과로 중단",
    "cancelled": "새 커밋이 올라와 중단",
    "error": "⚠️ pytest를 실행하지 못함",
}


def render_test_section(run: PytestRun) -> str:
    lines = ["### 테스트", "", f"**{_HEADLINES[run.status]}**"]
    s = run.summary
    if s is not None:
        lines += [
            "",
            f"전체 {s.total} · 통과 {s.passed} · 실패 {s.failed}"
            f" · 오류 {s.errors} · 건너뜀 {s.skipped}",
        ]
        if s.failed_tests:
            lines += ["", "실패한 테스트:"]
            lines += [f"- `{t}`" for t in s.failed_tests[:MAX_LISTED_FAILURES]]
            hidden = len(s.failed_tests) - MAX_LISTED_FAILURES
            if hidden > 0:
                lines.append(f"- 외 {hidden}개")
    if run.status == "error" and run.output:
        lines += [
            "",
            "<details><summary>출력</summary>",
            "",
            "```",
            run.output[-1500:],
            "```",
            "",
            "</details>",
        ]
    return "\n".join(lines) + "\n"
