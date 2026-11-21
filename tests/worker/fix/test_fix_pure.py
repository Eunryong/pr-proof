import pytest

from app.worker.fix.fix_pure import code_fix_tests_ok, out_of_scope, same_test_result
from app.worker.fix.scope_pure import StatusEntry, parse_porcelain
from app.worker.test_run_pure import PytestRun, ReportSummary


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


# --- 모델 수정 뒤 테스트 ---


def _code_run(passed: int, total: int, failed: int = 0, flaky: tuple[str, ...] = ()) -> PytestRun:
    summary = ReportSummary(passed=passed, failed=failed, total=total)
    return PytestRun(exit_code=1 if failed else 0, summary=summary, flaky=flaky)


def test_code_fix_tests_must_all_pass_with_same_count() -> None:
    before = _code_run(9, 9)
    assert code_fix_tests_ok(before, _code_run(9, 9))
    assert not code_fix_tests_ok(before, _code_run(8, 9, failed=1))
    assert not code_fix_tests_ok(before, _code_run(8, 8))  # 테스트가 사라짐
    assert not code_fix_tests_ok(before, PytestRun(exit_code=5, summary=None))  # 테스트 없음
