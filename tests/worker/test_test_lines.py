"""고친 위반의 테스트 표시를 상태와 파일마다 묶는다 (docs/DESIGN.md 4.9)."""

from app.worker.generated_tests_pure import GeneratedTest, LineRun
from app.worker.test_lines import MAX_LINES_SHOWN, generated_lines, status_lines
from app.worker.violation_pure import Violation


def _v(file: str, line: int) -> Violation:
    return Violation(id=f"{file}:{line}", rule_id="R", file=file, line=line, message="m",
                     found_by="checker")  # fmt: skip


def test_one_line_per_status_with_line_numbers_per_file() -> None:
    vs = [_v("a.py", 3), _v("a.py", 1), _v("b.py", 2), _v("a.py", 9)]
    runs = [LineRun("a.py:3", "generated"), LineRun("a.py:1", "generated"),
            LineRun("b.py:2", "existing"),
            LineRun("a.py:9", "none", "`@team` 파일을 쓰는 최상위 코드")]  # fmt: skip
    assert status_lines(runs, vs) == [
        "- 있던 테스트가 실행함 (1건): `b.py` 줄 2",
        "- 만든 테스트가 실행함 (2건): `a.py` 줄 1, 3",
        "- 있던 테스트도 만든 테스트도 이 줄을 실행하지 않음 (1건): `a.py` 줄 9 "
        "(모델이 적은 까닭: `` `@team` 파일을 쓰는 최상위 코드 ``)",
    ]


def test_long_line_lists_are_cut() -> None:
    vs = [_v("a.py", n) for n in range(1, MAX_LINES_SHOWN + 6)]
    (line,) = status_lines([LineRun(v.id, "existing") for v in vs], vs)
    assert line.endswith("외 5곳")


def test_generated_lines_count_kept_tests_and_name_dropped_ones() -> None:
    tests = [
        GeneratedTest("tests/test_pipeline_a.py", 5, "passed", covers=("x", "y")),
        GeneratedTest("tests/test_pipeline_b.py", 5, "passed", covers=("y",)),
        GeneratedTest("tests/test_pipeline_c.py", 5, "failed", rejected="fails_before"),
    ]
    assert generated_lines(tests) == [
        "- 만든 테스트 2개 (대상 위반 2건을 실행함): `tests/test_pipeline_a.py`, "
        "`tests/test_pipeline_b.py`",
        "- 뺀 테스트 1개: `tests/test_pipeline_c.py` (고치기 전 코드에서 실패함)",
    ]
    assert generated_lines([]) == []
