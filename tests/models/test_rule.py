import re
from datetime import UTC, datetime

from app.models.checker import Checker, checker_id
from app.models.rule import (
    CheckMethod,
    DroppedRule,
    Example,
    Rule,
    RuleSet,
    RuleSource,
    UnreadDoc,
    rule_id,
)
from app.models.violation import violation_id


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
