import re
from datetime import UTC, datetime
from typing import Final

import pytest

from app.worker.core.checker_pure import Checker, checker_id
from app.worker.core.rule_pure import (
    CheckMethod,
    DroppedRule,
    Example,
    Rule,
    RuleSet,
    RuleSource,
    UnreadDoc,
    checked_exclude,
    in_scope,
    rule_id,
)
from app.worker.core.violation_pure import violation_id


def _ruleset() -> RuleSet:
    rule = Rule(
        id="R-readme-0123abcd",
        text="`orders/` 아래 모듈은 `print`를 쓰지 않는다.",
        source=RuleSource("README.md", "코드 규칙 > 출력", "b" * 40),
        origin="document",
        scope=("orders/**",),
        check=CheckMethod(kind="tool", tool="ruff", params={"codes": ["T201"]}, reason=""),
        fix="model",
        examples=(
            Example("print(1)\n", (1,), "document"),
            Example("log.info(1)\n", (), "model"),
        ),
    )
    return RuleSet(
        rules=(rule,),
        sources=(("README.md", "b" * 40),),
        pipeline_version="1",
        prompt_version="1",
        model="claude-haiku-5-5",
        unread=(UnreadDoc("CONTRIBUTING.md", "크기 상한을 넘음"),),
        dropped=(DroppedRule("README.md", "구조", "지어낸 문장", "원문이 그 절에 없음"),),
    )


def test_ruleset_json_round_trip() -> None:
    ruleset = _ruleset()
    assert RuleSet.from_json(ruleset.to_json()) == ruleset


def test_empty_ruleset_round_trip() -> None:
    empty = RuleSet(rules=(), sources=(), pipeline_version="1", prompt_version="1", model="m")
    assert RuleSet.from_json(empty.to_json()) == empty


def test_rule_id_format_and_stability() -> None:
    rid = rule_id("README.md", "서비스 모듈은 print를 쓰지 않는다.")
    assert re.fullmatch(r"R-readme-[0-9a-f]{8}", rid)
    # 공백과 강조 기호의 차이는 같은 원문이다
    assert rid == rule_id("README.md", "  서비스 모듈은 `print`를\n  쓰지 않는다. ")
    assert rid == rule_id("README.md", "서비스 모듈은 **print**를 쓰지 않는다.")
    assert rid != rule_id("README.md", "서비스 모듈은 print를 쓰지 않는다")
    assert (
        rid.split("-")[2] == rule_id("CONTRIBUTING.md", "서비스 모듈은 print를 쓰지 않는다.")[-8:]
    )


def test_rule_id_file_abbreviation() -> None:
    assert rule_id("CONTRIBUTING.md", "x" * 10).startswith("R-contributing-")
    assert rule_id("docs/Team Rules.v2.md", "x" * 10).startswith("R-team-rules-v2-")
    assert rule_id("docs/한글.md", "x" * 10).startswith("R-doc-")


def test_violation_id_ignores_line_number_and_counts_repeats() -> None:
    first = violation_id("R-readme-1", "a.py", "    print(x)", 0)
    assert first == violation_id("R-readme-1", "a.py", "print(x)  ", 0)
    assert first != violation_id("R-readme-1", "a.py", "print(x)", 1)
    assert first != violation_id("R-readme-2", "a.py", "print(x)", 0)
    assert first != violation_id("R-readme-1", "b.py", "print(x)", 0)


def test_checker_id_is_body_hash() -> None:
    body = "id: no-print\nlanguage: python\nrule:\n  pattern: print($$$)\n"
    checker = Checker(
        id=checker_id(body), rule_id="R-readme-1", rule_text_hash="h", kind="ast-grep",
        body=body, examples=(), engine_version="0.51.3",
        verified_at=datetime(2026, 11, 18, tzinfo=UTC),
    )  # fmt: skip
    assert checker.id == checker_id(body) != checker_id(body + "\n")


@pytest.mark.parametrize(
    ("scope", "path", "inside"),
    [
        ((), "anything.py", True),
        (("orders/**",), "orders/a.py", True),
        (("orders/**",), "orders/services/a.py", True),
        (("orders/**",), "ordersx/a.py", False),
        (("orders/**",), "tests/orders/a.py", False),
        (("orders/*.py",), "orders/a/b.py", False),
        (("**/test_*.py",), "test_a.py", True),
        (("**/test_*.py",), "tests/unit/test_a.py", True),
        (("orders/models/**", "orders/api/**"), "orders/api/h.py", True),
        (("orders/[a-c].py",), "orders/b.py", True),
        (("orders/[!a].py",), "orders/a.py", False),
    ],
)
def test_in_scope(scope: tuple[str, ...], path: str, inside: bool) -> None:
    assert in_scope(scope, path) is inside


def _plain(exclude: tuple[str, ...], check: str = "checker") -> Rule:
    return Rule(
        id="R-x", text="규칙", source=RuleSource("README.md", "절", "0"), origin="document",
        scope=(), check=CheckMethod(kind=check), fix="none", exclude=exclude, exclude_reason="이유",
    )  # fmt: skip


BASE_FILES: Final = ("orders/a.py", "orders/b.py", "orders/c.py", "orders/test_a.py", "README.md")


def test_exclusion_that_removes_under_half_is_kept() -> None:
    rule = _plain(("**/test_*.py",))
    assert checked_exclude(rule, BASE_FILES) == rule  # Python 파일 4개 중 1개


@pytest.mark.parametrize("glob", ["**", "**/*.py", "orders/**"])
def test_exclusion_that_removes_half_or_all_is_dropped_and_recorded(glob: str) -> None:
    checked = checked_exclude(_plain((glob,)), BASE_FILES)
    assert (checked.exclude, checked.exclude_reason) == ((), "")
    assert glob in checked.exclude_dropped
    assert "4개 중 4개" in checked.exclude_dropped


def test_exactly_half_is_dropped() -> None:
    files = ("a.py", "b.py", "tests/test_a.py", "tests/test_b.py")
    assert checked_exclude(_plain(("tests/**",)), files).exclude == ()


def test_only_the_wide_glob_is_dropped() -> None:
    checked = checked_exclude(_plain(("**/test_*.py", "**/*.py")), BASE_FILES)
    assert checked.exclude == ("**/test_*.py",)
    assert checked.exclude_reason == "이유"
    assert "**/*.py" in checked.exclude_dropped


def test_narrow_globs_that_together_remove_half_are_all_dropped() -> None:
    checked = checked_exclude(_plain(("orders/a.py", "orders/b.py")), BASE_FILES)
    assert checked.exclude == ()
    assert "합쳐서" in checked.exclude_dropped


def test_without_applicable_files_only_a_plain_folder_glob_is_kept() -> None:
    checked = checked_exclude(_plain(("tests/**", "**/*.py")), ("README.md",))
    assert checked.exclude == ("tests/**",)
    assert "잴 수 없음" in checked.exclude_dropped


def test_files_the_rule_does_not_check_are_not_counted() -> None:
    # 내용을 보는 규칙은 Python 파일만, 경로 규칙은 모든 파일을 센다
    files = ("a.py", "b.py", "c.py", "docs/x.md", "docs/y.md", "docs/z.md", "docs/w.md")
    assert checked_exclude(_plain(("docs/**",)), files).exclude == ("docs/**",)
    assert checked_exclude(_plain(("docs/**",), check="path"), files).exclude == ()
