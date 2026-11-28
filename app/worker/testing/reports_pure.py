"""테스트 결과 파일 읽기: JUnit XML.

pytest `--junitxml`, ctest `--output-junit`, GoogleTest `--gtest_output=xml`이 내는 방언을 읽는다.
결과 파일은 샌드박스에서 PR 코드가 도는 동안 만들어지므로 믿을 수 없는 입력으로 다룬다.
DTD·엔티티를 거부하는 파서(defusedxml)로 읽고 크기를 제한하며, 형식이 어긋나면 None이다.
파일 읽기는 호출하는 쪽이 한다.
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal
from xml.etree.ElementTree import Element, ParseError

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring

from app.worker.testing.test_run_pure import TestReport

MAX_JUNIT_BYTES: Final = 50 * 1024 * 1024  # 큰 테스트 묶음의 결과도 들어가게 (report.json과 같다)
# testcase의 status 속성: ctest는 실패를 자식 없이 status="fail"로만 적기도 한다
_SKIPPED_STATUS: Final = frozenset({"notrun", "disabled"})
_ROOTS: Final = frozenset({"testsuites", "testsuite"})

Outcome = Literal["passed", "failed", "error", "skipped"]


@dataclass(frozen=True)
class TestCase:
    """테스트 하나. id는 classname과 name을 점으로 이은 것(같으면 name), file은 저장소 기준 경로이고
    결과 파일에 없으면 ""다."""

    __test__ = False  # 이름이 Test로 시작해도 pytest가 테스트 클래스로 모으지 않게

    id: str
    file: str
    outcome: Outcome


def read_junit(
    data: bytes, *, workdir: str = "/work", max_bytes: int = MAX_JUNIT_BYTES
) -> tuple[TestCase, ...] | None:
    """JUnit XML의 testcase를 차례대로 읽는다. 크거나 깨졌거나 DTD·엔티티가 있으면 None."""
    if len(data) > max_bytes:
        return None
    try:
        root = fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except (ParseError, DefusedXmlException, ValueError):
        return None
    if root.tag not in _ROOTS:
        return None
    cases = []
    for case in root.iter("testcase"):
        name = case.get("name")
        if not name:
            return None
        classname = case.get("classname") or ""
        cases.append(
            TestCase(
                id=f"{classname}.{name}" if classname and classname != name else name,
                file=case.get("file", "").removeprefix(f"{workdir}/"),
                outcome=_outcome(case),
            )
        )
    return tuple(cases)


def _outcome(case: Element) -> Outcome:
    if case.find("error") is not None:
        return "error"
    if case.find("failure") is not None or case.get("status") == "fail":
        return "failed"
    if case.find("skipped") is not None or case.get("status") in _SKIPPED_STATUS:
        return "skipped"
    return "passed"


def summarize(cases: Sequence[TestCase]) -> TestReport:
    """테스트마다의 결과를 수로 센다. failed_tests는 실패와 오류의 id다."""
    n = Counter(c.outcome for c in cases)
    return TestReport(
        passed=n["passed"],
        failed=n["failed"],
        errors=n["error"],
        skipped=n["skipped"],
        total=len(cases),
        failed_tests=tuple(c.id for c in cases if c.outcome in ("failed", "error")),
    )
