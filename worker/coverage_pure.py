"""위반 줄 커버리지 (docs/DESIGN.md 4.5, F8). 수정 판단은 보지 않고, 고친 위반의 표시(4.9)에 쓴다.

`coverage.xml`은 샌드박스에서 PR 코드가 도는 동안 만들어지므로 믿을 수 없는 입력으로 다룬다.
DTD·엔티티를 거부하는 파서(defusedxml)로 읽고 크기를 제한하며, 형식이 조금이라도 어긋나면
"모름"(None)으로 돌려준다. 파일 읽기는 호출하는 쪽(`app/worker/coverage.py`)이 한다.
"""

import ast
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Final
from xml.etree.ElementTree import Element, ParseError

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring

from worker.violation_pure import Violation

# 이보다 큰 coverage.xml은 읽지 않는다 (모름으로 본다)
MAX_COVERAGE_XML_BYTES: Final = 20 * 1024 * 1024


@dataclass(frozen=True)
class FileCoverage:
    """파일 하나의 줄 커버리지. measured는 coverage.py가 실행 단위로 센 줄(문장의 첫 줄)이다."""

    measured: frozenset[int]
    executed: frozenset[int]


@dataclass(frozen=True)
class CoverageData:
    """리포 기준 경로별 줄 커버리지. 테스트가 import하지 않은 파일은 들어 있지 않다."""

    files: Mapping[str, FileCoverage]


def _normalize(path: str) -> str:
    return path.replace("\\", "/").removeprefix("./")


def parse_coverage_xml(
    data: bytes, *, max_bytes: int = MAX_COVERAGE_XML_BYTES
) -> CoverageData | None:
    """Cobertura 형식 coverage.xml을 읽는다. 크거나 깨졌거나 DTD·엔티티가 있으면 None."""
    if len(data) > max_bytes:
        return None
    try:
        root = fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except (ParseError, DefusedXmlException, ValueError):
        return None
    if root.tag != "coverage":
        return None
    measured: dict[str, set[int]] = {}
    executed: dict[str, set[int]] = {}
    for clazz in root.iter("class"):
        filename = clazz.get("filename")
        if not filename:
            return None
        path = _normalize(filename)
        file_measured = measured.setdefault(path, set())
        file_executed = executed.setdefault(path, set())
        for line in _lines(clazz):
            parsed = _parse_line(line)
            if parsed is None:
                return None
            number, hits = parsed
            file_measured.add(number)
            if hits > 0:
                file_executed.add(number)
    return CoverageData(
        files={
            path: FileCoverage(frozenset(lines), frozenset(executed[path]))
            for path, lines in measured.items()
        }
    )


def _lines(clazz: Element) -> list[Element]:
    return clazz.findall("./lines/line")


def _parse_line(line: Element) -> tuple[int, int] | None:
    try:
        number = int(line.get("number", ""))
        hits = int(line.get("hits", ""))
    except ValueError:
        return None
    if number < 1 or hits < 0:
        return None
    return number, hits


@dataclass(frozen=True)
class _Stmt:
    start: int  # 데코레이터가 있으면 그 줄부터
    end: int
    depth: int
    lines: tuple[int, ...]  # coverage.py가 이 문장을 셀 수 있는 줄 (데코레이터 줄, 문장 첫 줄)
    first_child: int | None  # 안에 든 첫 문장의 시작 줄 (복합문의 머리 범위를 정한다)


def _statements(source: str) -> list[_Stmt] | None:
    """소스의 모든 문장과 줄 범위. 파싱할 수 없으면 None. 코드를 실행하지 않는다."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    result: list[_Stmt] = []

    def visit(node: ast.AST, depth: int) -> int | None:
        """node 아래 문장을 모으고, 그중 가장 먼저 시작하는 문장의 줄을 돌려준다."""
        first: int | None = None
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                decorators = [d.lineno for d in getattr(child, "decorator_list", [])]
                start = min([child.lineno, *decorators])
                inner = visit(child, depth + 1)
                result.append(
                    _Stmt(
                        start=start,
                        end=child.end_lineno or child.lineno,
                        depth=depth,
                        lines=(*decorators, child.lineno),
                        first_child=inner,
                    )
                )
            else:
                start = visit(child, depth)
            if start is not None and (first is None or start < first):
                first = start
        return first

    try:
        visit(tree, 0)
    except RecursionError:
        return None
    return result


def line_covered(file_cov: FileCoverage, line: int, statements: list[_Stmt] | None) -> bool:
    """줄 하나가 테스트로 실행됐는지.

    1. coverage.py가 센 줄이면 그 결과를 쓴다.
    2. 아니면 소스(statements)에서 그 줄을 감싸는 가장 안쪽 문장을 찾는다. 여러 줄 문장의
       이어지는 줄과 복합문의 머리(여러 줄 `if (...)`, 데코레이터와 `def` 사이)는 그 문장의
       첫 줄(데코레이터가 있으면 데코레이터 줄부터)의 결과를 쓴다.
    3. 그 밖(복합문 몸통 사이의 빈 줄·주석, `else:`·`except:` 줄, 소스 없음, 파싱 실패,
       `# pragma: no cover`로 빠진 문장)은 실행되지 않은 것으로 본다. 확실하지 않으면 자동
       수정 대상에서 빼는 쪽을 고른다.
    """
    if line in file_cov.measured:
        return line in file_cov.executed
    if statements is None:
        return False
    containing = [s for s in statements if s.start <= line <= s.end]
    if not containing:
        return False
    stmt = max(containing, key=lambda s: (s.depth, -s.start))
    if stmt.first_child is not None and line >= stmt.first_child:
        return False  # 복합문 몸통 안이지만 어느 문장에도 속하지 않는 줄
    for candidate in stmt.lines:
        if candidate in file_cov.measured:
            return candidate in file_cov.executed
    return False


def mark_covered(
    findings: Iterable[Violation],
    coverage: CoverageData | None,
    sources: Mapping[str, str] | None = None,
) -> tuple[Violation, ...]:
    """위반마다 covered를 채운다.

    - coverage가 None(파일 없음·깨짐): 모두 None(모름)
    - coverage.xml에 없는 파일: False (테스트가 import하지 않았다)
    - 그 밖: `line_covered`의 규칙. sources는 리포 기준 경로별 소스이고,
      소스가 없는 파일은 2단계를 건너뛴다
    """
    findings = tuple(findings)
    if coverage is None:
        return tuple(replace(f, covered=None) for f in findings)
    sources = sources or {}
    parsed: dict[str, list[_Stmt] | None] = {}
    result = []
    for f in findings:
        path = _normalize(f.file)
        file_cov = coverage.files.get(path)
        if file_cov is None:
            result.append(replace(f, covered=False))
            continue
        if path not in parsed:
            source = sources.get(f.file, sources.get(path))
            parsed[path] = _statements(source) if source is not None else None
        result.append(replace(f, covered=line_covered(file_cov, f.line, parsed[path])))
    return tuple(result)


# 수정안 PR과 결과 코멘트가 고친 위반마다 붙이는 글 (4.9). 테스트가 그 줄을 실행했는지만 말한다.
# 실행하지 않은 줄은 테스트가 고친 동작을 확인하지 않았다는 뜻이다
COVERAGE_LABEL: Final[Mapping[bool | None, str]] = {
    True: "테스트가 이 줄을 실행함",
    False: "이 줄을 실행하는 테스트가 없음",
    None: "테스트가 이 줄을 실행하는지 알 수 없음",
}
