from typing import Final

import pytest

from app.models.finding import Finding, SkillResult
from app.models.repo_config import RepoConfig
from app.models.test_run import PytestRun, ReportSummary
from app.worker import report
from app.worker.base_config import BaseConfig
from app.worker.report import format_rules_line, format_test_line, render_result

SYMBOLS: Final = ("⚠️", "✅", "❌")
HEAD: Final = "a" * 40


def _base(memory_gb: float = 1, max_memory_gb: float = 4, problems=()) -> BaseConfig:
    return BaseConfig(
        repo=RepoConfig(),
        memory_gb=memory_gb,
        max_memory_gb=max_memory_gb,
        ruff_config=".pipeline-out/base/pyproject.toml",
        ruff_source="pyproject.toml",
        problems=tuple(problems),
    )


def _finding(file: str = "orders/a.py", line: int = 3, code: str = "F401") -> Finding:
    return Finding(
        file=file,
        line=line,
        severity="error",
        message="`os` imported but unused",
        skill="lint",
        source="pyproject.toml",
        code=code,
    )


def _render(run: PytestRun, lint: SkillResult | None = None, base: BaseConfig | None = None):
    return render_result(run, HEAD, 3.0, lint=lint, base=base or _base())


PASSED: Final = PytestRun(exit_code=0, summary=ReportSummary(passed=9, total=9))


@pytest.mark.parametrize(
    ("run", "line"),
    [
        (PASSED, "통과 (9개)"),
        (
            PytestRun(exit_code=1, summary=ReportSummary(passed=7, failed=1, errors=1, total=9)),
            "실패 (9개 중 2개)",
        ),
        (PytestRun(exit_code=5, summary=ReportSummary()), "실행된 테스트 없음"),
        (PytestRun(exit_code=124, summary=None, timed_out=True), "시간 초과로 중단"),
        (PytestRun(exit_code=None, summary=None, cancelled=True), "새 커밋이 올라와 중단"),
        (PytestRun(exit_code=137, summary=None), "실행 불가 (메모리 한도 1GB를 넘어 종료됨)"),
    ],
    ids=["passed", "failed", "no_tests", "timeout", "cancelled", "unrunnable"],
)
def test_test_line_is_text_only(run: PytestRun, line: str) -> None:
    assert format_test_line(run) == line
    text = _render(run)
    assert f"**테스트:** {line}\n" in text
    assert not any(symbol in text for symbol in SYMBOLS)  # 기호는 규칙 줄에만 쓴다


def test_passed_shows_counts_without_failures() -> None:
    text = _render(PASSED)
    assert "전체 9 · 통과 9 · 실패 0 · 오류 0 · 건너뜀 0" in text
    assert "실패한 테스트" not in text


def test_failed_lists_tests() -> None:
    summary = ReportSummary(passed=1, failed=1, total=2, failed_tests=("tests/test_a.py::test_x",))
    assert "- `tests/test_a.py::test_x`" in _render(PytestRun(exit_code=1, summary=summary))


def test_collection_error_lists_module() -> None:
    summary = ReportSummary(errors=1, failed_tests=("tests/test_broken.py",))
    text = _render(PytestRun(exit_code=2, summary=summary))
    assert "**테스트:** 실패 (0개 중 1개)" in text
    assert "- `tests/test_broken.py`" in text


def test_long_failure_list_is_truncated() -> None:
    count = report.MAX_LISTED_FAILURES + 3
    ids = tuple(f"tests/test_a.py::test_{i}" for i in range(count))
    run = PytestRun(exit_code=1, summary=ReportSummary(failed=count, total=count, failed_tests=ids))
    text = _render(run)
    assert f"`tests/test_a.py::test_{report.MAX_LISTED_FAILURES - 1}`" in text
    assert f"`tests/test_a.py::test_{report.MAX_LISTED_FAILURES}`" not in text
    assert "- 외 3개" in text


def test_unrunnable_shows_output_without_counts() -> None:
    text = _render(PytestRun(exit_code=4, summary=None, output="ERROR: usage"))
    assert "실행 불가 (pytest 설정·인자 오류)" in text
    assert "ERROR: usage" in text
    assert "전체" not in text


def test_timeout_hides_output() -> None:
    assert "<details>" not in _render(PytestRun(exit_code=124, summary=None, timed_out=True))


def test_memory_exceeded_points_to_base_setting() -> None:
    text = _render(PytestRun(exit_code=137, summary=None), base=_base(memory_gb=2))
    assert "**테스트:** 실행 불가 (메모리 한도 2GB를 넘어 종료됨)" in text
    assert "base 브랜치의 `.pipeline.yaml`에서 `sandbox.memory_gb`로 4GB까지" in text


def test_memory_exceeded_at_max_has_no_raise_hint() -> None:
    text = _render(PytestRun(exit_code=137, summary=None), base=_base(4, 4))
    assert "상한(4GB)" in text
    assert "늘릴 수 있습니다" not in text


def test_fractional_memory_limit() -> None:
    assert "메모리 한도 1.5GB" in _render(PytestRun(exit_code=137, summary=None), base=_base(1.5))


def test_timeout_137_has_no_memory_hint() -> None:
    assert "메모리 한도는" not in _render(PytestRun(exit_code=137, summary=None, timed_out=True))


# --- 규칙 줄 --------------------------------------------------------------------


def test_rules_line_with_violations() -> None:
    lint = SkillResult(skill="lint", findings=(_finding(), _finding(line=9)), source="x")
    assert format_rules_line([lint]) == "⚠️ 위반 2건 (lint 2)"


def test_rules_line_clean() -> None:
    assert format_rules_line([SkillResult(skill="lint", source="x")]) == "✅ 위반 없음"


def test_unchecked_skill_has_no_symbol() -> None:
    line = format_rules_line([SkillResult(skill="lint", error="ruff 실행 오류")])
    assert line == "검사하지 못함 (lint)"
    assert not any(symbol in line for symbol in SYMBOLS)  # 검사 못함은 위반도 통과도 아니다


def test_result_has_two_top_lines_then_details() -> None:
    lint = SkillResult(skill="lint", findings=(_finding(),), source="pyproject.toml")
    text = _render(PASSED, lint=lint)
    lines = text.splitlines()
    assert lines[2] == "**테스트:** 통과 (9개)"
    assert lines[3] == "**규칙:** ⚠️ 위반 1건 (lint 1)"
    assert "- `orders/a.py:3` `F401` `os` imported but unused" in text
    assert "적용한 규칙: ruff (pyproject.toml)" in text


def test_failed_tests_still_show_rules() -> None:
    lint = SkillResult(skill="lint", findings=(_finding(),), source="x")
    text = _render(PytestRun(exit_code=1, summary=ReportSummary(failed=1, total=1)), lint=lint)
    assert "**규칙:** ⚠️ 위반 1건" in text


def test_long_findings_list_is_truncated() -> None:
    count = report.MAX_LISTED_FINDINGS + 2
    lint = SkillResult(skill="lint", findings=tuple(_finding(line=i) for i in range(count)))
    text = _render(PASSED, lint=lint)
    assert f"⚠️ 위반 {count}건" in text  # 건수는 전체
    assert "- 외 2건" in text


def test_config_problems_are_listed() -> None:
    text = _render(PASSED, base=_base(problems=["`.pipeline.yaml`을 읽지 못해 기본값을 씀: x"]))
    assert "- `.pipeline.yaml`을 읽지 못해 기본값을 씀: x" in text


def test_install_failure_keeps_rules() -> None:
    lint = SkillResult(skill="lint", findings=(_finding(),), source="x")
    text = report.render_install_failure(HEAD, "ERROR: No matching distribution", lint=lint)
    assert "**테스트:** 실행 불가 (의존성을 설치하지 못함)" in text
    assert "**규칙:** ⚠️ 위반 1건" in text
    assert "No matching distribution" in text


def test_environment_wait_shows_no_results() -> None:
    text = report.render_environment_wait(HEAD, final=False)
    assert "**규칙:**" not in text


def test_flaky_tests_are_listed_separately() -> None:
    summary = ReportSummary(passed=8, failed=1, total=9, failed_tests=("t::a",))
    text = _render(PytestRun(exit_code=1, summary=summary, flaky=("t::a",)))
    assert "**테스트:** 통과, 불안정한 테스트 1개" in text
    assert "불안정한 테스트 (처음엔 실패, 다시 돌려 통과):\n- `t::a`" in text
    assert "실패한 테스트:" not in text


def test_code_fence_outgrows_backticks_in_text() -> None:
    assert report.code_fence("plain") == "```"
    assert report.code_fence('x = """\n```py\n"""') == "````"


def _ready_fix(tmp_path, patch: str):
    from app.models.fix import FixOutcome

    path = tmp_path / "fix.patch"
    path.write_text(patch)
    return FixOutcome(skill="lint", status="ready", fixed=1, patch_path=path)


def test_patch_with_backticks_keeps_fence_closed(tmp_path) -> None:
    lines = report.fix_section(_ready_fix(tmp_path, '-"""\n-```\n+x\n'))
    text = "\n".join(lines)
    assert "````diff" in text and text.rstrip().endswith("````\n\n</details>".rstrip())


def test_secret_blocked_proposal_hides_patch(tmp_path) -> None:
    from app.models.proposal import ProposalResult, push_blocker

    reason = push_blocker("+token = ghp_" + "a" * 36 + "\n", ["a.py"])
    result = ProposalResult(reason=reason)
    assert result.secret_blocked
    text = "\n".join(report.fix_section(_ready_fix(tmp_path, "+token\n"), result))
    assert "제안 PR을 만들지 않았습니다 (비밀값으로 보이는 내용이 있음" in text
    assert "수정 내용" not in text


# --- 3단계: static, 커버리지, AI 수정 ---


def test_suffix_lists_every_waiting_skill() -> None:
    proposals = {"lint": ("approval", None), "static": ("approval", None)}
    assert report.proposal_suffix(proposals) == " · 자동 수정 가능: `/fix lint`, `/fix static`"
    assert report.proposal_suffix({"static": ("tests", None)}) == ""
    assert report.proposal_suffix("approval") == " · 자동 수정 가능: `/fix lint`"  # 2단계 호출


def test_diff_coverage_line() -> None:
    from app.worker.coverage import DiffCoverage

    assert report.diff_coverage_line(None) == []
    assert report.diff_coverage_line(DiffCoverage(66.67, 3, 1)) == [
        "",
        "바뀐 줄 커버리지 (참고): 66.67% (테스트가 센 3줄 중 1줄이 실행되지 않음)",
    ]


def test_ai_fix_section_fences_explanation_and_shows_cost() -> None:
    from app.models.fix import FixOutcome

    fix = FixOutcome(
        skill="static", status="ready", fixed=1, test_run=PASSED,
        explanation="```\n@team 끝", ai_calls=3, ai_cost_usd=0.4123,
        models=("haiku", "haiku", "sonnet"),
    )  # fmt: skip
    lines = "\n".join(report.fix_section(fix))
    assert "AI 호출 3회 (haiku, haiku, sonnet) · 추정 비용 $0.4123" in lines
    assert "````\n```\n@team 끝\n````" in lines  # 설명 안의 ```가 블록을 닫지 못한다
    failed = FixOutcome(skill="static", status="ai_failed", detail="남은 위반 1건", ai_calls=3,
                        ai_cost_usd=0.1, models=("haiku",) * 3)  # fmt: skip
    text = "\n".join(report.fix_section(failed))
    assert "정해진 횟수 안에 검증을 통과하지 못함 (남은 위반 1건)" in text and "AI 호출 3회" in text


def test_gate_details_lists_fixable_locations_only_once() -> None:
    from app.models.coverage import FixGate

    def f(line: int) -> Finding:
        return Finding(
            "a.py", line, "warning", "repeated `2`", "static", "기본 규칙", "STATIC-REPEAT"
        )

    fixable = tuple(f(n) for n in range(report.MAX_LISTED_FINDINGS + 2))
    gate = FixGate(allowed=True, fixable=fixable, uncovered=(f(999),))
    lines = report.gate_details(gate)
    assert "자동 수정 대상이 아닌 위반 1건: 테스트가 지나가지 않음 1건" in lines
    (target,) = [line for line in lines if "이 고칠 위반" in line]
    assert target.startswith(f"`/fix static`이 고칠 위반 {len(fixable)}건: `a.py:0`, `a.py:1`")
    assert target.endswith(" 외 2건") and "repeated" not in target
    assert report.gate_details(FixGate(allowed=True, fixable=fixable)) == []  # 전부 대상이면 생략


def test_file_and_test_names_cannot_leave_their_code_span() -> None:
    """파일 이름과 테스트 이름은 PR 작성자가 정한다. 백틱으로 스팬을 닫고 멘션을 넣지 못한다."""
    nasty = "orders/`@team`<b>.py"
    assert report.finding_line(_finding(file=nasty)).startswith(f"- ``{nasty}:3`` `F401` ")
    assert f"``{nasty}:3``" in report.target_locations("lint", [_finding(file=nasty)])
    assert report.finding_line(_finding()).startswith("- `orders/a.py:3` `F401` ")
