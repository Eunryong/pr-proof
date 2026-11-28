"""수정 작성의 모델이 만든 테스트 (docs/DESIGN.md 4.7, 4.8의 6번, 7.9). 데이터, 받아들이는 조건,
위반 줄의 실행 상태, 도구 결과의 글. 판단만 하고 테스트를 돌리거나 파일을 바꾸지 않는다.

테스트를 돌리고 결과 파일을 읽는 것은 `app/worker/lang/python.py`, 도구와 출구 검사의 실행은
`app/worker/fix/model_fix.py`가 한다. 받아들이는 조건은 저장소 주인이 정했다(2026-11-21, 11.1):
고치기 전 코드에서 통과한다, 위반이 있는 줄을 실행한다, 있던 테스트(와 앞서 받아들인 테스트)가
실행하지 않던 줄을 새로 실행한다, 고친 뒤에도 통과한다. 여기에 빈 테스트가 아니다(테스트 함수마다
assert나 `pytest.raises`)를 더했다.
"""

import ast
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from app.worker.core.violation_pure import Violation
from app.worker.testing.coverage_pure import FileCoverage, line_covered, statements
from app.worker.testing.test_run_pure import TestRun

# 만든 테스트의 상한 (9.2). 수정의 크기 상한(파일 20개, 바뀐 줄 500줄)과 따로 센다. 테스트 줄이
# 수정의 상한을 먹어 수정이 못 올라가는 일이 없게 한다
MAX_TEST_FILES: Final = 10
MAX_TEST_LINES: Final = 200  # 파일 하나
MAX_TOTAL_TEST_LINES: Final = 1000
# 도구 결과에 싣는 글의 상한 (글자 수). PR 코드가 만든 글이라 자료로만 넣는다
MAX_OUTPUT_CHARS: Final = 3000
MAX_LISTED: Final = 20

Rejection = Literal[
    "empty", "too_many", "too_big", "not_run", "fails_before", "misses_violation", "overlaps"
]
REJECTION_LABEL: Final[Mapping[str, str]] = {
    "empty": "assert나 pytest.raises가 없는 테스트 함수가 있음",
    "too_many": f"만든 테스트 파일 상한({MAX_TEST_FILES}개)을 넘음",
    "too_big": f"테스트 줄 상한(파일마다 {MAX_TEST_LINES}줄, 합 {MAX_TOTAL_TEST_LINES}줄)을 넘음",
    "not_run": "pytest가 이 파일의 테스트를 하나도 돌리지 않음",
    "fails_before": "고치기 전 코드에서 실패함",
    "misses_violation": "대상 위반의 줄을 하나도 실행하지 않음",
    "overlaps": "있던 테스트나 앞서 받아들인 테스트가 이미 실행하는 줄만 실행함",
}

Outcome = Literal["passed", "failed", "not_run"]


@dataclass(frozen=True)
class FileLines:
    """소스 파일 하나에서 테스트 파일마다 실행한 줄. by의 값은 그 줄을 실행한 테스트 파일들이고 ""는
    테스트가 아니라 모듈을 불러올 때(수집) 실행된 것이다."""

    measured: frozenset[int]
    by: Mapping[int, frozenset[str]]


@dataclass(frozen=True)
class Suite:
    """샌드박스에서 pytest를 한 번 돈 결과 중 판단에 쓰는 것. outcomes와 counts는 테스트 파일마다,
    lines는 대상 위반이 있는 소스 파일마다다."""

    run: TestRun
    outcomes: Mapping[str, Outcome]
    counts: Mapping[str, int]
    lines: Mapping[str, FileLines]


@dataclass(frozen=True)
class GeneratedTest:
    """만든 테스트 파일 하나의 기록 (7.9). covers는 고치기 전 코드에서 이 파일이 실행한 대상
    위반 id다. 파이프라인이 실행으로 정하고, 모델이 무엇을 위해 썼다고 말한 것은 쓰지 않는다."""

    file: str
    lines: int
    before: Outcome
    covers: tuple[str, ...] = ()
    new_lines: int = 0  # 있던 테스트와 앞서 받아들인 테스트가 실행하지 않던 줄 수
    after: Outcome | None = None  # 고친 뒤. 고치기 전에 떨어졌으면 None
    rejected: Rejection | None = None

    @property
    def accepted(self) -> bool:
        return self.rejected is None


LineStatus = Literal["existing", "generated", "none", "unknown"]
# 고친 위반마다의 표시 (4.9). "이 줄을 실행하는 테스트가 없음"은 "테스트 자체가 없다"로 읽혀 지웠다
LINE_LABEL: Final[Mapping[str, str]] = {
    "existing": "있던 테스트가 실행함",
    "generated": "만든 테스트가 실행함",
    "none": "있던 테스트도 만든 테스트도 이 줄을 실행하지 않음",
    "unknown": "테스트가 이 줄을 실행하는지 알 수 없음",
}


@dataclass(frozen=True)
class LineRun:
    """대상 위반 하나의 실행 상태. why는 어느 테스트도 실행하지 않는 줄에 모델이 적은 까닭이다."""

    violation_id: str
    status: LineStatus
    why: str = ""


STATE_TYPES: Final = (
    ("app.worker.testing.generated_tests_pure", "GeneratedTest"),
    ("app.worker.testing.generated_tests_pure", "LineRun"),
)


# --- 결과 파일 읽기 (파일은 python.py가 읽어 넘긴다) ---


def file_of_test(context: str) -> str:
    """pytest-cov의 테스트 문맥(`tests/test_a.py::TestX::test_y|run`)에서 테스트 파일."""
    return context.split("::", 1)[0].split("|", 1)[0]


def parse_report(data: object) -> tuple[dict[str, Outcome], dict[str, int]]:
    """pytest-json-report의 report.json에서 테스트 파일마다 (결과, 테스트 수). 하나라도 실패·오류면
    실패다. 수집에 실패한 파일은 테스트가 0개이고 실패다. 모양이 틀린 항목은 건너뛴다."""
    outcomes: dict[str, Outcome] = {}
    counts: dict[str, int] = {}
    if not isinstance(data, dict):
        return outcomes, counts
    for test in data.get("tests") or []:
        if not isinstance(test, dict) or not isinstance(test.get("nodeid"), str):
            continue
        file = file_of_test(test["nodeid"])
        counts[file] = counts.get(file, 0) + 1
        failed = test.get("outcome") in ("failed", "error")
        if failed or outcomes.get(file) == "failed":
            outcomes[file] = "failed"
        elif test.get("outcome") == "passed":
            outcomes[file] = "passed"
        else:
            outcomes.setdefault(file, "not_run")  # skipped 등
    for collector in data.get("collectors") or []:
        if isinstance(collector, dict) and collector.get("outcome") == "failed":
            nodeid = collector.get("nodeid")
            if isinstance(nodeid, str) and nodeid:
                outcomes[file_of_test(nodeid)] = "failed"
    return outcomes, counts


def parse_contexts(data: bytes, files: Iterable[str]) -> dict[str, FileLines] | None:
    """coverage.py JSON 보고서(`show_contexts`)에서 files의 줄마다 실행한 테스트 파일.
    깨졌으면 None."""
    try:
        report = json.loads(data)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    entries = report.get("files") if isinstance(report, dict) else None
    if not isinstance(entries, dict):
        return None
    out: dict[str, FileLines] = {}
    for file in files:
        entry = entries.get(file)
        if not isinstance(entry, dict):
            continue
        try:
            measured = frozenset(
                int(n) for n in [*entry.get("executed_lines", []), *entry.get("missing_lines", [])]
            )
            by = {
                int(line): frozenset(file_of_test(c) if c else "" for c in contexts)
                for line, contexts in (entry.get("contexts") or {}).items()
            }
        except (TypeError, ValueError, AttributeError):
            return None
        out[file] = FileLines(measured, by)
    return out


# --- 받아들이는 조건 (4.8의 6번) ---


def _is_test_function(node: ast.AST) -> bool:
    return isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test")


def _checks_something(func: ast.AST) -> bool:
    for node in ast.walk(func):
        if isinstance(node, ast.Assert):
            return True
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if name in ("raises", "warns") or name.startswith("assert"):
                return True
    return False


def has_checks(source: str) -> bool:
    """테스트 함수가 하나 이상이고 모두 assert, `pytest.raises`·`warns`, `assert*` 호출 중 하나를
    가진다. 프로그램이 볼 수 있는 최소 조건이고 테스트의 질을 보지는 않는다 (BACKLOG)."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return False
    funcs = [n for n in tree.body if _is_test_function(n)]
    funcs += [
        m for n in tree.body if isinstance(n, ast.ClassDef) and n.name.startswith("Test")
        for m in n.body if _is_test_function(m)
    ]  # fmt: skip
    return bool(funcs) and all(_checks_something(f) for f in funcs)


def _own_lines(lines: FileLines | None, test: str) -> frozenset[int]:
    if lines is None:
        return frozenset()
    return frozenset(n for n, by in lines.by.items() if test in by)


def _executes(v: Violation, test: str, suite: Suite, sources: Mapping[str, str]) -> bool:
    """고치기 전 코드에서 test가 위반 v의 줄을 실행했는지. 모듈을 불러올 때 실행된 줄("")은 test가
    그 파일의 다른 줄을 실행했고 있던 테스트가 그 줄을 실행하지 않았을 때만 test의 것으로 본다."""
    lines = suite.lines.get(v.file)
    own = _own_lines(lines, test)
    if lines is None or not own:
        return False
    on_import = frozenset(n for n, by in lines.by.items() if "" in by)
    executed = own | (on_import if v.covered is not True else frozenset())
    source = sources.get(v.file)
    stmts = statements(source) if source is not None else None
    return line_covered(FileCoverage(lines.measured, executed), v.line, stmts)


@dataclass(frozen=True)
class Candidate:
    file: str
    source: str


def judge_tests(
    candidates: Sequence[Candidate],
    targets: Sequence[Violation],
    before: Suite,
    sources: Mapping[str, str],
    *,
    kept: Sequence[str] = (),
) -> tuple[GeneratedTest, ...]:
    """고치기 전 코드에 만든 테스트를 얹어 돈 결과로 파일마다 받아들일지 정한다.

    sources는 대상 위반이 있는 파일의 고치기 전 소스다. kept는 앞 회차에 받아들인 테스트 파일이다.
    그 파일을 먼저, 나머지는 이름순으로 본다. 앞에서 받아들인 테스트가 실행한 줄만 다시 실행하는
    테스트는 받지 않는다(회차를 넘어 겹치지 않는다). 고친 뒤의 결과(after)는 `after_problem`이 본다.
    """
    order = sorted(candidates, key=lambda c: (c.file not in kept, c.file))
    generated = {c.file for c in candidates}
    tested = set(before.counts) | set(before.outcomes)
    existing: set[tuple[str, int]] = set()
    for file, lines in before.lines.items():
        for test in {t for by in lines.by.values() for t in by if t and t not in generated}:
            existing |= {(file, n) for n in _own_lines(lines, test)}
    taken: set[tuple[str, int]] = set()
    total_lines = accepted = 0
    out: list[GeneratedTest] = []
    for c in order:
        size = len(c.source.splitlines())
        outcome: Outcome = before.outcomes.get(c.file, "not_run") if c.file in tested else "not_run"
        covers = tuple(v.id for v in targets if _executes(v, c.file, before, sources))
        own = {(f, n) for f, lines in before.lines.items() for n in _own_lines(lines, c.file)}
        newly = own | {
            (v.file, v.line) for v in targets if v.id in covers and v.covered is not True
        }
        fresh = newly - existing - taken
        rejected: Rejection | None = None
        if not has_checks(c.source):
            rejected = "empty"
        elif accepted >= MAX_TEST_FILES:
            rejected = "too_many"
        elif size > MAX_TEST_LINES or total_lines + size > MAX_TOTAL_TEST_LINES:
            rejected = "too_big"
        elif outcome == "not_run" or not before.counts.get(c.file):
            rejected = "not_run"
        elif outcome == "failed":
            rejected = "fails_before"
        elif not covers:
            rejected = "misses_violation"
        elif not fresh:
            rejected = "overlaps"
        if rejected is None:
            accepted += 1
            total_lines += size
            taken |= newly
        out.append(GeneratedTest(c.file, size, outcome, covers, len(fresh), rejected=rejected))
    return tuple(out)


@dataclass(frozen=True)
class AfterCheck:
    """고친 뒤의 결과 (4.8의 6번). broken은 고치기 전에 통과했는데 고친 뒤 실패한 만든 테스트다."""

    tests: tuple[GeneratedTest, ...]
    existing_ok: bool
    broken: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.existing_ok and not self.broken


def after_check(tests: Sequence[GeneratedTest], before: TestRun, after: Suite) -> AfterCheck:
    """있던 테스트는 모두 통과하고 수정 전과 같은 수가 돌아야 한다. 받아들인 만든 테스트는 고친
    뒤에도 통과해야 한다. 고치기 전에 통과한 테스트가 고친 뒤 실패하면 수정이 동작을 바꾼 것이라,
    테스트를 빼지 않고 수정을 다시 쓰게 한다(호출하는 쪽이 회차를 넘긴다). broken이 있으면
    existing_ok는 보지 않는다(만든 테스트의 실패로 전체 상태가 실패다)."""
    filled = tuple(
        _with_after(t, after.outcomes.get(t.file, "not_run")) if t.accepted else t for t in tests
    )
    broken = tuple(t.file for t in filled if t.accepted and t.after != "passed")
    run, report = after.run, after.run.report
    generated = sum(after.counts.get(t.file, 0) for t in filled if t.accepted)
    existing_ok = (
        run.allows_code_fix
        and before.report is not None
        and report is not None
        and (run.passed_count - generated, report.total - generated)
        == (before.passed_count, before.report.total)
    )
    return AfterCheck(filled, existing_ok, broken)


def _with_after(t: GeneratedTest, outcome: Outcome) -> GeneratedTest:
    return GeneratedTest(t.file, t.lines, t.before, t.covers, t.new_lines, outcome, t.rejected)


def line_runs(
    targets: Sequence[Violation],
    tests: Sequence[GeneratedTest],
    untested: Mapping[tuple[str, str], str] | None = None,
) -> tuple[LineRun, ...]:
    """대상 위반마다의 실행 상태. 있던 테스트가 실행했으면(`Violation.covered`, 수정 전 테스트 단계)
    existing, 받아들인 만든 테스트가 실행했으면 generated, 그 밖은 none(커버리지를 읽지 못했으면
    unknown)이다. untested는 (파일, 규칙) → 모델이 적은 까닭이다."""
    covered_by_new = {vid for t in tests if t.accepted for vid in t.covers}
    out = []
    for v in targets:
        if v.covered is True:
            out.append(LineRun(v.id, "existing"))
        elif v.id in covered_by_new:
            out.append(LineRun(v.id, "generated"))
        else:
            why = (untested or {}).get((v.file, v.rule_id), "")
            out.append(LineRun(v.id, "unknown" if v.covered is None else "none", why))
    return tuple(out)


def group_lines(
    runs: Sequence[LineRun], violations: Sequence[Violation]
) -> list[tuple[LineStatus, str, tuple[int, ...], str]]:
    """표시를 (상태, 파일, 줄 번호들, 까닭)으로 묶는다. 같은 글이 위반마다 되풀이되지 않게 한다.
    상태 순서는 LINE_LABEL의 순서, 그 안은 처음 나온 파일 순서다."""
    by_id = {v.id: v for v in violations}
    groups: dict[tuple[str, str, str], list[int]] = {}
    for r in runs:
        v = by_id.get(r.violation_id)
        if v is not None:
            groups.setdefault((r.status, v.file, r.why), []).append(v.line)
    order = list(LINE_LABEL)
    keys = sorted(groups, key=lambda k: order.index(k[0]))
    return [(s, f, tuple(sorted(set(groups[(s, f, w)]))), w) for s, f, w in keys]  # type: ignore[misc]


# --- 도구 결과의 글 (모델에 자료로 넘긴다) ---


def clip(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= limit else f"… (앞 {len(text) - limit}자 생략)\n" + text[-limit:]


def _violation_lines(prefix: str, violations: Sequence[Violation]) -> list[str]:
    shown = [f"- {v.file}:{v.line} 규칙 {v.rule_id}: {v.message}" for v in violations[:MAX_LISTED]]
    more = len(violations) - MAX_LISTED
    return [f"{prefix} {len(violations)}건", *shown, *([f"- 외 {more}건"] if more > 0 else [])]


def render_check(
    left: Sequence[Violation], new: Sequence[Violation], unchecked: Sequence[str], by_model: int
) -> str:
    """규칙 검사 도구의 결과 글."""
    lines = []
    if unchecked:
        lines += ["규칙 검사를 하지 못한 규칙:", *(f"- {u}" for u in unchecked)]
    lines += _violation_lines("남은 대상 위반", left) + _violation_lines("새로 생긴 위반", new)
    if by_model:
        lines.append(
            f"모델 판단으로 찾은 대상 위반 {by_model}건은 도구로 다시 확인할 수 없다. 이 결과는 "
            "그 줄이 바뀌었는지만 본다."
        )
    if not (left or new or unchecked):
        lines.append("남은 대상 위반과 새 위반이 없다.")
    return "\n".join(lines)


def _run_line(run: TestRun) -> str:
    s = run.report
    if s is None:
        return f"테스트 결과를 읽지 못함 (상태 {run.status})"
    counts = f"통과 {run.passed_count}, 실패 {s.failed}, 오류 {s.errors}, 전체 {s.total}"
    return f"상태 {run.status}: {counts}"


def render_before(
    tests: Sequence[GeneratedTest], suite: Suite, targets: Sequence[Violation]
) -> str:
    """run_tests(code="before")의 결과 글: 고치기 전 코드에 만든 테스트를 얹어 돈 결과와, 출구
    검사가 그 테스트를 받아들일지."""
    lines = [f"고치기 전 코드 + 만든 테스트: {_run_line(suite.run)}"]
    if not tests:
        lines.append("만든 테스트 파일이 없다. 새 테스트 파일은 정한 자리에만 만든다.")
    by_id = {v.id: v for v in targets}
    for t in tests:
        state = "받아들임" if t.rejected is None else f"받지 않음 ({REJECTION_LABEL[t.rejected]})"
        where = ", ".join(f"{by_id[i].file}:{by_id[i].line}" for i in t.covers if i in by_id)
        lines.append(
            f"- {t.file}: {state}. 고치기 전 {t.before}, 실행한 대상 위반 {len(t.covers)}건"
            + (f" ({where})" if where else "")
            + f", 새로 실행한 줄 {t.new_lines}줄"
        )
    left = [v for v in targets if v.covered is not True
            and not any(v.id in t.covers for t in tests if t.accepted)]  # fmt: skip
    if left:
        lines += _violation_lines("있던 테스트도 받아들인 테스트도 실행하지 않는 대상 위반", left)
    failed = suite.run.report.failed_tests if suite.run.report else ()
    if failed:
        lines += ["실패한 테스트:", *(f"- {n}" for n in failed[:MAX_LISTED])]
    if suite.run.status != "passed" and suite.run.output:
        lines += ["pytest 출력 (끝 부분):", clip(suite.run.output)]
    return "\n".join(lines)


def render_now(suite: Suite, generated: Sequence[str]) -> str:
    """run_tests(code="now")의 결과 글: 지금 파일 상태로 모든 테스트를 돈 결과."""
    lines = [f"지금 코드 + 모든 테스트: {_run_line(suite.run)}"]
    for file in generated:
        lines.append(f"- 만든 테스트 {file}: {suite.outcomes.get(file, 'not_run')}")
    failed = suite.run.report.failed_tests if suite.run.report else ()
    if failed:
        lines += ["실패한 테스트:", *(f"- {n}" for n in failed[:MAX_LISTED])]
    if suite.run.status != "passed" and suite.run.output:
        lines += ["pytest 출력 (끝 부분):", clip(suite.run.output)]
    return "\n".join(lines)
