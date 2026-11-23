"""app/worker/doc_rules_pure.py: 위반의 규칙 id로 그 규칙을 찾는다."""

from app.worker.core.rule_pure import CheckMethod, Rule, RuleSource
from app.worker.core.tools_pure import RuffConfig, config_rule
from app.worker.core.violation_pure import Violation
from app.worker.rules.doc_rules_pure import (
    CallUsage,
    CheckerRef,
    Found,
    RuleChecked,
    outcomes_by_rule,
)


def test_tool_settings_violations_point_at_the_rule_set() -> None:
    config = config_rule(RuffConfig("ruff.toml", "", "b"))
    doc = Rule(id="R-readme-1", text="t", source=RuleSource("README.md", "s", "a"),
               origin="document", scope=(), check=CheckMethod("checker"), fix="model")  # fmt: skip
    ref = CheckerRef(key="k", id="i", kind="ruff", cached=True)
    violations = (
        Violation(id="1", rule_id="F401", file="a.py", line=1, message="m", found_by="tool",
                  code="F401"),
        Violation(id="2", rule_id="R-readme-1", file="a.py", line=2, message="m",
                  found_by="tool", code="T201"),
    )  # fmt: skip
    found = Found((RuleChecked(doc, ref), RuleChecked(config, ref)), violations, 0,
                  CallUsage(0, 0, 0, (), 0, 0))  # fmt: skip
    by_rule = outcomes_by_rule(found)
    assert by_rule["F401"].rule is config and by_rule["R-readme-1"].rule is doc
    assert "T201" not in by_rule  # 문서 규칙의 위반은 그 규칙의 id를 쓴다
