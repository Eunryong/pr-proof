from typing import Final

import pytest

from app.worker.config.base_config import BaseConfig
from app.worker.config.repo_config_pure import RepoConfig
from app.worker.core.tools_pure import RuffConfig, RuffSetup, config_rule
from app.worker.core.violation_pure import Violation, violation_id
from app.worker.report import report
from app.worker.report.report import render_result
from app.worker.rules.doc_rules_pure import CallUsage, CheckerRef, Found, RuleChecked
from app.worker.testing.test_lines import format_test_line
from app.worker.testing.test_run_pure import NO_PROFILE_WHY, StepRun, TestReport, TestRun

from ..helpers import pytest_run

SYMBOLS: Final = ("⚠️", "✅", "❌")
HEAD: Final = "a" * 40


def _base(memory_gb: float = 1, max_memory_gb: float = 4, problems=()) -> BaseConfig:
    return BaseConfig(
        repo=RepoConfig(),
        memory_gb=memory_gb,
        max_memory_gb=max_memory_gb,
        ruff=RuffSetup("0.17.5"),
        problems=tuple(problems),
    )


CONFIG_RULE: Final = config_rule(RuffConfig("pyproject.toml", "[tool.ruff]\n", "b" * 40))


def _found(*lines: int, file: str = "orders/a.py", message: str = "`os` imported but unused"):
    """도구 설정의 규칙 묶음(ruff)이 찾은 F401 위반들."""
    violations = tuple(
        Violation(
            id=violation_id("F401", file, f"import os  # {n}", 0), rule_id="F401", file=file,
            line=n, message=message, found_by="tool", code="F401",
            line_text=f"import os  # {n}", mechanical_fix=True,
        )
        for n in lines
    )  # fmt: skip
    ref = CheckerRef(key="tool-config:k", id="c" * 64, kind="ruff", cached=True)
    return Found((RuleChecked(CONFIG_RULE, ref),), violations, 0, CallUsage(0, 10, 0, (), 0, 0))


def _render(run: TestRun, doc_rules=None, base: BaseConfig | None = None):
    return render_result(run, HEAD, 3.0, doc_rules=doc_rules, base=base or _base())


PASSED: Final = pytest_run(0, TestReport(passed=9, total=9))
BUILD_FAILED: Final = TestRun(build=(StepRun("cmake --build build", 2, output="error: x"),))


@pytest.mark.parametrize(
    ("run", "line"),
    [
        (PASSED, "통과 (9개)"),
        (
            pytest_run(1, TestReport(passed=7, failed=1, errors=1, total=9)),
            "실패 (9개 중 2개)",
        ),
        (pytest_run(5, TestReport()), "실행된 테스트 없음"),
        (pytest_run(124, None, timed_out=True), "시간 초과로 중단"),
        (pytest_run(None, None, cancelled=True), "새 커밋이 올라와 중단"),
        (pytest_run(137, None), "실행 불가 (메모리 한도 1GB를 넘어 종료됨)"),
        (
            TestRun(unplanned=NO_PROFILE_WHY),
            f"돌리지 않음 ({NO_PROFILE_WHY})",
        ),
        (TestRun(unplanned="C++ 환경이 없음"), "돌리지 않음 (C++ 환경이 없음)"),
        (
            BUILD_FAILED,
            "빌드 실패로 테스트를 돌리지 못함 (`cmake --build build` 단계, 종료 코드 2)",
        ),
    ],
    ids=[
        "passed", "failed", "no_tests", "timeout", "cancelled", "unrunnable", "no_env",
        "no_env-other-reason", "build_failed",
    ],
)  # fmt: skip
def test_test_line_is_text_only(run: TestRun, line: str) -> None:
    assert format_test_line(run) == line
    text = _render(run)
    assert f"**테스트:** {line}\n" in text
    assert not any(symbol in text for symbol in SYMBOLS)  # 기호는 규칙 줄에만 쓴다


def test_passed_shows_counts_without_failures() -> None:
    text = _render(PASSED)
    assert "전체 9 · 통과 9 · 실패 0 · 오류 0 · 건너뜀 0" in text
    assert "실패한 테스트" not in text


def test_failed_lists_tests() -> None:
    summary = TestReport(passed=1, failed=1, total=2, failed_tests=("tests/test_a.py::test_x",))
    assert "- `tests/test_a.py::test_x`" in _render(pytest_run(1, summary))


def test_collection_error_lists_module() -> None:
    summary = TestReport(errors=1, failed_tests=("tests/test_broken.py",))
    text = _render(pytest_run(2, summary))
    assert "**테스트:** 실패 (0개 중 1개)" in text
    assert "- `tests/test_broken.py`" in text


def test_long_failure_list_is_truncated() -> None:
    count = report.MAX_LISTED_FAILURES + 3
    ids = tuple(f"tests/test_a.py::test_{i}" for i in range(count))
    run = pytest_run(1, TestReport(failed=count, total=count, failed_tests=ids))
    text = _render(run)
    assert f"`tests/test_a.py::test_{report.MAX_LISTED_FAILURES - 1}`" in text
    assert f"`tests/test_a.py::test_{report.MAX_LISTED_FAILURES}`" not in text
    assert "- 외 3개" in text


def test_unrunnable_shows_output_without_counts() -> None:
    text = _render(pytest_run(4, None, output="ERROR: usage"))
    assert "실행 불가 (pytest 설정·인자 오류)" in text
    assert "ERROR: usage" in text
    assert "전체" not in text


def test_timeout_hides_output() -> None:
    assert "<details>" not in _render(pytest_run(124, None, timed_out=True))


def test_memory_exceeded_points_to_base_setting() -> None:
    text = _render(pytest_run(137, None), base=_base(memory_gb=2))
    assert "**테스트:** 실행 불가 (메모리 한도 2GB를 넘어 종료됨)" in text
    assert "base 브랜치의 `.pipeline.yaml`에서 `sandbox.memory_gb`로 4GB까지" in text


def test_memory_exceeded_at_max_has_no_raise_hint() -> None:
    text = _render(pytest_run(137, None), base=_base(4, 4))
    assert "상한(4GB)" in text
    assert "늘릴 수 있습니다" not in text


def test_fractional_memory_limit() -> None:
    assert "메모리 한도 1.5GB" in _render(pytest_run(137, None), base=_base(1.5))


def test_timeout_137_has_no_memory_hint() -> None:
    assert "메모리 한도는" not in _render(pytest_run(137, None, timed_out=True))


# --- 규칙 줄 --------------------------------------------------------------------


def test_result_has_two_top_lines_then_details() -> None:
    text = _render(PASSED, doc_rules=_found(3))
    lines = text.splitlines()
    top = lines.index("**테스트:** 통과 (9개)")
    assert lines[top + 1] == "**규칙:** ⚠️ 위반 1건 (규칙 1개)"
    assert "- `F401` `pyproject.toml의 설정대로 ruff를 통과한다` · 1건" in text
    assert "  출처: `pyproject.toml > [tool.ruff]` · 도구 `ruff` (`pyproject.toml`의 설정)" in text
    assert "  - `orders/a.py:3` `` `os` imported but unused ``" in text
    assert text.count("**규칙:**") == 1  # 규칙 결과를 한 줄로 합쳤다 (4.9)


def test_failed_tests_still_show_rules() -> None:
    run = pytest_run(1, TestReport(failed=1, total=1))
    assert "**규칙:** ⚠️ 위반 1건" in _render(run, doc_rules=_found(3))


def test_rule_line_is_absent_before_the_rule_path_ran() -> None:
    assert "**규칙:**" not in _render(PASSED)


def test_config_problems_are_listed() -> None:
    text = _render(PASSED, base=_base(problems=["`.pipeline.yaml`을 읽지 못해 기본값을 씀: x"]))
    assert "- `.pipeline.yaml`을 읽지 못해 기본값을 씀: x" in text


def test_install_failure_keeps_rules() -> None:
    text = report.render_install_failure(
        HEAD, "ERROR: No matching distribution", doc_rules=_found(3)
    )
    assert "**테스트:** 실행 불가 (의존성을 설치하지 못함)" in text
    assert "**규칙:** ⚠️ 위반 1건" in text
    assert "No matching distribution" in text


NO_TESTS: Final = pytest_run(5, TestReport())


@pytest.mark.parametrize(
    ("run", "note"),
    [
        (NO_TESTS, "pytest가 테스트를 하나도 찾지 못해 고친 것을 확인할 수 없으므로 모델 "
                   "수정안을 만들지 않습니다. 지금 테스트를 돌릴 수 있는 것은 Python(pytest) "
                   "테스트뿐입니다."),
        (pytest_run(124, None, timed_out=True),
         "테스트가 시간 안에 끝나지 않아 고친 것을 확인할 수 없으므로 수정안을 만들지 않습니다."),
        (pytest_run(4, None, output="ERROR: usage"),
         "테스트 결과를 얻지 못해 고친 것을 확인할 수 없으므로 수정안을 만들지 않습니다."),
        (TestRun(unplanned=NO_PROFILE_WHY),
         "테스트를 돌릴 수 없어 고친 것을 확인할 수 없으므로 수정안을 만들지 않습니다."),
        (BUILD_FAILED,
         "빌드가 실패해 고친 것을 확인할 수 없으므로 수정안을 만들지 않습니다."),
    ],
    ids=["no_tests", "timeout", "unrunnable", "no_env", "build_failed"],
)  # fmt: skip
def test_untested_run_with_violations_says_why_no_fix(run: TestRun, note: str) -> None:
    """테스트가 돌지 않았으면 위반이 있을 때 "테스트:" 줄 아래(펼친 부분)에 까닭을 적는다. 맨 위
    줄은 짧게 둔다."""
    text = _render(run, doc_rules=_found(3))
    top, _, rest = text.partition("**규칙:**")
    assert note not in top and f"\n\n{note}\n" in rest.partition("<details>")[0]
    assert note not in _render(run, doc_rules=_found())  # 위반이 없으면 할 일이 없다
    assert note not in _render(run)


def test_runs_that_ran_or_stopped_have_no_untested_note() -> None:
    failed = pytest_run(1, TestReport(failed=1, total=1))
    cancelled = pytest_run(None, None, cancelled=True)
    for run in (PASSED, failed, cancelled):
        assert report.untested_note(run, True) == []
    assert report.untested_note(NO_TESTS, False) == []


def test_install_failure_with_violations_says_no_fix_was_judged() -> None:
    text = report.render_install_failure(HEAD, "ERROR: x", doc_rules=_found(3))
    assert f"\n\n{report.INSTALL_FAILED_NOTE}\n" in text
    assert report.INSTALL_FAILED_NOTE not in report.render_install_failure(
        HEAD, "ERROR: x", doc_rules=_found()
    )


def test_environment_wait_shows_no_results() -> None:
    text = report.render_environment_wait(HEAD, final=False)
    assert "**규칙:**" not in text


def test_flaky_tests_are_listed_separately() -> None:
    summary = TestReport(passed=8, failed=1, total=9, failed_tests=("t::a",))
    text = _render(pytest_run(1, summary, flaky=("t::a",)))
    assert "**테스트:** 통과, 불안정한 테스트 1개" in text
    assert "불안정한 테스트 (처음엔 실패, 다시 돌려 통과):\n- `t::a`" in text
    assert "실패한 테스트:" not in text


def test_code_fence_outgrows_backticks_in_text() -> None:
    assert report.code_fence("plain") == "```"
    assert report.code_fence('x = """\n```py\n"""') == "````"


def test_diff_coverage_line() -> None:
    from app.worker.testing.coverage import DiffCoverage

    assert report.diff_coverage_line(None) == []
    assert report.diff_coverage_line(DiffCoverage(66.67, 3, 1)) == [
        "",
        "바뀐 줄 커버리지 66.67% (참고, 위반이 아님): 있던 테스트가 PR이 바꾼 줄 3줄 중 2줄을 "
        "실행함",
    ]


def test_clean_pr_comment_is_short_and_keeps_coverage_folded() -> None:
    """위반이 없으면 결론 두 줄 뒤에 펼친 것이 없다. 테스트 수와 커버리지는 접는 블록 안에 있다."""
    from app.worker.report.doc_rules_report import DETAILS_SUMMARY
    from app.worker.testing.coverage import DiffCoverage

    text = report.render_result(PASSED, "a" * 40, 1.0, doc_rules=_found(),
                                diff_coverage=DiffCoverage(0.0, 220, 220))  # fmt: skip
    shown, _, folded = text.partition(f"<details><summary>{DETAILS_SUMMARY}</summary>")
    assert shown.splitlines()[2:4] == [
        "**테스트:** 통과 (9개)",
        "**규칙:** ✅ 위반 없음 · 규칙 1개를 확인함 (도구·검사기 1개)",
    ]
    assert "커버리지" not in shown and "220줄 중 0줄을 실행함" in folded


def test_file_names_and_tool_messages_cannot_leave_their_code_span() -> None:
    """파일 이름은 PR 작성자가, ruff의 메시지는 코드의 식별자로 만들어진다. 백틱으로 스팬을 닫고
    멘션을 넣지 못한다 (T10)."""
    nasty = "orders/`@team`<b>.py"
    text = _render(PASSED, doc_rules=_found(3, file=nasty, message="`x` @team <b>bad</b>"))
    assert f"  - ``{nasty}:3`` `` `x` @team <b>bad</b> ``" in text
