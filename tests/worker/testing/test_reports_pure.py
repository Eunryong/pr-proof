"""app/worker/testing/reports_pure.py: JUnit XML 읽기 (pytest, ctest, GoogleTest 방언)."""

import pytest

from app.worker.testing.reports_pure import TestCase, read_junit, summarize
from app.worker.testing.test_run_pure import TestReport

# pytest --junitxml (xunit2). 수집 오류는 error가 달린 testcase로 남는다
PYTEST = b"""<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="1" failures="1" skipped="1" tests="5">
<testcase classname="tests.test_a" name="test_ok" time="0.001" />
<testcase classname="tests.test_a" name="test_bad" time="0.001">
  <failure message="assert 1 == 2">tests/test_a.py:7: AssertionError</failure>
</testcase>
<testcase classname="tests.test_a" name="test_skip" time="0">
  <skipped type="pytest.skip" message="later">skip</skipped>
</testcase>
<testcase classname="tests.test_a" name="test_teardown" time="0.001">
  <failure message="x" /><error message="teardown" />
</testcase>
<testcase classname="" name="tests.test_broken" time="0">
  <error message="collection failure">ImportError</error>
</testcase>
</testsuite></testsuites>
"""

# ctest --output-junit. 실패는 status="fail"만 있고 자식이 없을 수 있다
CTEST = b"""<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="Linux-c++" tests="4" failures="1" disabled="1" skipped="0" time="1">
  <testcase name="VersionTest.Basic" classname="VersionTest.Basic" time="0.01" status="run">
    <system-out>[ PASSED ]</system-out>
  </testcase>
  <testcase name="EngineTest.Load" classname="EngineTest.Load" time="0.02" status="fail">
    <system-out>Expected: 3</system-out>
  </testcase>
  <testcase name="Slow" classname="Slow" time="0" status="disabled" />
  <testcase name="Gpu" classname="Gpu" time="0" status="notrun">
    <skipped message="no gpu"/>
  </testcase>
</testsuite>
"""

# GoogleTest --gtest_output=xml. file은 컴파일한 경로라 작업 폴더가 붙어 있다
GTEST = b"""<?xml version="1.0" encoding="UTF-8"?>
<testsuites tests="4" failures="1" disabled="1" errors="0" time="0.001" name="AllTests">
  <testsuite name="MathTest" tests="4" failures="1" disabled="1" skipped="1" errors="0">
    <testcase name="Add" file="/work/tests/math_test.cpp" line="5" status="run"
              result="completed" classname="MathTest" />
    <testcase name="Sub" file="/work/tests/math_test.cpp" line="9" status="run"
              result="completed" classname="MathTest">
      <failure message="math_test.cpp:10&#x0A;Expected equality" type=""><![CDATA[x]]></failure>
    </testcase>
    <testcase name="DISABLED_Mul" file="/work/tests/math_test.cpp" line="13" status="notrun"
              result="suppressed" classname="MathTest" />
    <testcase name="Later" file="/work/tests/math_test.cpp" line="17" status="run"
              result="skipped" classname="MathTest"><skipped message="later"/></testcase>
  </testsuite>
</testsuites>
"""


def test_pytest_dialect() -> None:
    cases = read_junit(PYTEST)
    assert cases == (
        TestCase("tests.test_a.test_ok", "", "passed"),
        TestCase("tests.test_a.test_bad", "", "failed"),
        TestCase("tests.test_a.test_skip", "", "skipped"),
        TestCase("tests.test_a.test_teardown", "", "error"),
        TestCase("tests.test_broken", "", "error"),
    )
    assert summarize(cases) == TestReport(
        passed=1, failed=1, errors=2, skipped=1, total=5,
        failed_tests=("tests.test_a.test_bad", "tests.test_a.test_teardown", "tests.test_broken"),
    )  # fmt: skip


def test_ctest_dialect_reads_failure_from_status() -> None:
    cases = read_junit(CTEST)
    assert cases == (
        TestCase("VersionTest.Basic", "", "passed"),
        TestCase("EngineTest.Load", "", "failed"),
        TestCase("Slow", "", "skipped"),
        TestCase("Gpu", "", "skipped"),
    )
    assert summarize(cases) == TestReport(
        passed=1, failed=1, skipped=2, total=4, failed_tests=("EngineTest.Load",)
    )


def test_gtest_dialect_keeps_the_file_relative_to_the_repo() -> None:
    cases = read_junit(GTEST)
    assert cases == (
        TestCase("MathTest.Add", "tests/math_test.cpp", "passed"),
        TestCase("MathTest.Sub", "tests/math_test.cpp", "failed"),
        TestCase("MathTest.DISABLED_Mul", "tests/math_test.cpp", "skipped"),
        TestCase("MathTest.Later", "tests/math_test.cpp", "skipped"),
    )
    assert summarize(cases).failed_tests == ("MathTest.Sub",)


def test_no_testcase_is_an_empty_result_not_a_broken_one() -> None:
    cases = read_junit(b"<testsuites />")
    assert cases == () and summarize(cases) == TestReport()


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not xml",
        b"<testsuites><testcase name='a'>",
        b"<coverage />",
        b"<testsuite><testcase classname='x' /></testsuite>",
    ],
    ids=["empty", "not-xml", "truncated", "other-root", "testcase-without-name"],
)
def test_malformed_input_is_unknown(data: bytes) -> None:
    assert read_junit(data) is None


def test_oversized_input_is_unknown() -> None:
    assert read_junit(CTEST, max_bytes=len(CTEST) - 1) is None
    assert read_junit(CTEST, max_bytes=len(CTEST)) is not None


@pytest.mark.parametrize(
    "data",
    [
        b"""<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>
<testsuite><testcase name="&lol2;" /></testsuite>""",
        b"""<?xml version="1.0"?>
<!DOCTYPE t [<!ENTITY secret SYSTEM "file:///etc/passwd">]>
<testsuite><testcase name="&secret;" /></testsuite>""",
        b"""<?xml version="1.0"?>
<!DOCTYPE testsuite SYSTEM "http://example.invalid/junit.dtd">
<testsuite><testcase name="a" /></testsuite>""",
    ],
    ids=["entity-expansion", "external-entity", "external-dtd"],
)
def test_dtd_and_entities_are_rejected(data: bytes) -> None:
    assert read_junit(data) is None
