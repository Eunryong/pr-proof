"""테스트 결과를 PR 코멘트용 마크다운으로 바꾼다. 판정을 새로 내지 않고 모델 값만 보여준다.

코멘트 맨 위는 "테스트:" 줄과 "규칙:" 줄이다. 테스트는 글자로만 쓰고, ⚠️/✅는 규칙 줄에만 쓴다
(docs/DESIGN.md F9). 규칙 줄은 2단계(lint 검출)에서 붙는다.
"""

from typing import Final

from app.models.test_run import PytestRun

MAX_LISTED_FAILURES: Final = 10
MAX_OUTPUT_CHARS: Final = 1500


def format_test_line(run: PytestRun) -> str:
    """'테스트:' 줄의 글자 표시. 기호를 쓰지 않는다."""
    s = run.summary
    match run.status:
        case "passed":
            return f"통과 ({s.total}개)" if s else "통과"
        case "failed":
            if s is None:
                return "실패"
            return f"실패 ({s.total}개 중 {s.failed + s.errors}개)"
        case "no_tests":
            return "실행된 테스트 없음"
        case "timeout":
            return "시간 초과로 중단"
        case "cancelled":
            return "새 커밋이 올라와 중단"
        case "unrunnable":
            return f"실행 불가 ({run.unrunnable_reason})"


def render_test_section(run: PytestRun) -> str:
    lines = [f"**테스트:** {format_test_line(run)}"]
    s = run.summary
    if s is not None and s.total:
        lines += [
            "",
            f"전체 {s.total} · 통과 {s.passed} · 실패 {s.failed}"
            f" · 오류 {s.errors} · 건너뜀 {s.skipped}",
        ]
    if s is not None and s.failed_tests:
        lines += ["", "실패한 테스트:"]
        lines += [f"- `{t}`" for t in s.failed_tests[:MAX_LISTED_FAILURES]]
        hidden = len(s.failed_tests) - MAX_LISTED_FAILURES
        if hidden > 0:
            lines.append(f"- 외 {hidden}개")
    if run.status == "unrunnable" and run.output:
        lines += [
            "",
            "<details><summary>출력</summary>",
            "",
            "```",
            run.output[-MAX_OUTPUT_CHARS:],
            "```",
            "",
            "</details>",
        ]
    return "\n".join(lines) + "\n"
