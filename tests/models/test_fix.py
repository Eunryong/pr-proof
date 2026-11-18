import pytest

from app.models.finding import Finding, SkillResult
from app.models.fix import (
    StatusEntry,
    code_fix_tests_ok,
    compare,
    lint_fix_blocker,
    out_of_scope,
    parse_porcelain,
    same_test_result,
    unresolved,
)
from app.models.test_run import PytestRun, ReportSummary


def _f(file="a.py", line=1, code="F401", message="m", fixable=True) -> Finding:
    return Finding(file, line, "error", message, "lint", "x", code, fixable)


def _run(code: int, passed: int = 9, total: int = 9) -> PytestRun:
    summary = ReportSummary(passed=passed, failed=total - passed, total=total)
    return PytestRun(exit_code=code, summary=summary)


def test_parse_porcelain_with_rename_and_untracked() -> None:
    out = " M a.py\0R  new.py\0old.py\0?? tests/x.py\0 D gone.py\0"
    assert parse_porcelain(out) == [
        StatusEntry(" M", "a.py"),
        StatusEntry("R ", "new.py", "old.py"),
        StatusEntry("??", "tests/x.py"),
        StatusEntry(" D", "gone.py"),
    ]


@pytest.mark.parametrize(
    ("entries", "bad"),
    [
        ([StatusEntry(" M", "a.py")], []),
        ([StatusEntry(" M", "b.py")], [" M b.py".strip()]),  # 위반이 없던 파일
        ([StatusEntry("??", "tests/conftest.py")], ["?? tests/conftest.py"]),  # 새 파일
        ([StatusEntry(" D", "a.py")], ["D a.py"]),  # 삭제
        ([StatusEntry("R ", "a.py", "old.py")], ["R a.py"]),  # 이름 변경
        ([StatusEntry("??", ".pipeline-out/report.json")], []),  # 파이프라인 산출물
        ([StatusEntry(" M", ".github/workflows/ci.yml")], ["M .github/workflows/ci.yml"]),
    ],
    ids=["allowed", "other-file", "new-file", "delete", "rename", "outputs", "github"],
)
def test_out_of_scope(entries, bad) -> None:
    assert out_of_scope(entries, ["a.py"]) == bad


def test_compare_ignores_line_shift() -> None:
    before = [_f(line=3), _f(line=5, code="E711", message="cmp")]
    after = SkillResult(skill="lint", findings=(_f(line=2, code="E711", message="cmp"),))
    assert compare(before, after) == (1, [])


def test_compare_detects_new_violation() -> None:
    before = [_f()]
    new = _f(code="F841", message="unused var")
    fixed, added = compare(before, SkillResult(skill="lint", findings=(new,)))
    assert fixed == 1 and added == [new]


def test_compare_counts_duplicates() -> None:
    before = [_f(line=1)]
    after = SkillResult(skill="lint", findings=(_f(line=1), _f(line=2)))
    assert compare(before, after)[1] == [_f(line=2)]


@pytest.mark.parametrize(
    ("before", "after", "same"),
    [
        (_run(0), _run(0), True),
        (_run(0), _run(1, passed=8), False),
        (_run(0), _run(0, passed=8, total=8), False),  # 테스트 수가 줄었다
        (PytestRun(exit_code=5, summary=ReportSummary()), PytestRun(5, ReportSummary()), True),
        (_run(0), PytestRun(exit_code=5, summary=ReportSummary()), False),
    ],
    ids=["passed", "now-failing", "fewer-tests", "no-tests", "passed-to-none"],
)
def test_same_test_result(before, after, same) -> None:
    assert same_test_result(before, after) is same


def test_lint_fix_blocker() -> None:
    lint = SkillResult(skill="lint", findings=(_f(),))
    assert lint_fix_blocker(lint, _run(0)) is None
    assert lint_fix_blocker(lint, PytestRun(exit_code=5, summary=ReportSummary())) is None
    assert lint_fix_blocker(lint, _run(1, passed=8)) == "tests"
    unfixable = SkillResult(skill="lint", findings=(_f(fixable=False),))
    assert lint_fix_blocker(unfixable, _run(0)) == "no_fixable"
    assert lint_fix_blocker(SkillResult(skill="lint"), _run(0)) == "no_fixable"


# --- AI 수정 검증 (3단계) ---


def _static(file: str, code: str, line: int = 1) -> Finding:
    return Finding(file, line, "warning", f"{code} msg", "static", "기본 규칙", code)


def test_unresolved_counts_targets_by_key() -> None:
    a1, a2 = _static("a.py", "PLR2004", 3), _static("a.py", "PLR2004", 9)
    b = _static("b.py", "STATIC-REPEAT")
    before = [a1, a2, b]
    # a.py의 PLR2004 두 건 중 a1만 대상: 하나만 줄면 해소
    assert unresolved(before, [a1], SkillResult("static", (a2, b))) == []
    assert unresolved(before, [a1], SkillResult("static", (a1, a2, b))) == [a1]
    # 둘 다 대상이면 둘 다 없어져야 한다 (줄이 밀려도 같은 키)
    assert unresolved(before, [a1, a2], SkillResult("static", (_static("a.py", "PLR2004", 5),)))
    assert unresolved(before, [b], SkillResult("static", (a1, a2))) == []


def _code_run(passed: int, total: int, failed: int = 0, flaky: tuple[str, ...] = ()) -> PytestRun:
    summary = ReportSummary(passed=passed, failed=failed, total=total)
    return PytestRun(exit_code=1 if failed else 0, summary=summary, flaky=flaky)


def test_code_fix_tests_must_all_pass_with_same_count() -> None:
    before = _code_run(9, 9)
    assert code_fix_tests_ok(before, _code_run(9, 9))
    assert not code_fix_tests_ok(before, _code_run(8, 9, failed=1))
    assert not code_fix_tests_ok(before, _code_run(8, 8))  # 테스트가 사라짐
    assert not code_fix_tests_ok(before, PytestRun(exit_code=5, summary=None))  # 테스트 없음
