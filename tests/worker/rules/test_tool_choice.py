"""app/worker/rules/checkers.py의 도구 고르기 (docs/DESIGN.md 4.4 (a)): 도구 설정의 규칙 묶음은 모델
없이 ruff로 정하고, 도구를 고르는 프롬프트와 저장 키는 프로그램이 만든다. 모델을 부르지 않는다."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest

from app.worker.checker_authoring_pure import Checked, Unchecked
from app.worker.config.repo_config_pure import CallSettings, ModelSettings
from app.worker.core.checker_pure import tool_codes
from app.worker.core.rule_pure import CheckMethod, Rule, RuleSource
from app.worker.core.tools_pure import RuffConfig, RuffSetup, config_rule
from app.worker.rules import tool_choice_pure
from app.worker.rules.checkers import plan_and_author
from app.worker.store.budget import CallBudget
from app.worker.store.state import Store

from ...redis_stand_ins import MemoryKeys

NOW: Final = datetime(2026, 11, 20, 12, tzinfo=UTC)
UNUSED: Final = Rule(
    id="R-readme-2", text="안 쓰는 import를 남기지 않는다.",
    source=RuleSource("README.md", "코드 규칙 > import", "abc"), origin="document",
    scope=(), check=CheckMethod("checker"), fix="model",
)  # fmt: skip


def _no_model(prompt: str, **kwargs: object):
    pytest.fail("모델을 부르지 않아야 한다")


class _NoSandbox:
    def on_examples(self, *args: object):
        raise AssertionError("검사기를 돌리지 않아야 한다")

    def on_files(self, *args: object):
        raise AssertionError("검사기를 돌리지 않아야 한다")

    def rule_exists(self, code: str) -> bool:
        raise AssertionError("도구의 규칙 코드를 묻지 않아야 한다")


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "pipeline.db", MemoryKeys())


def _plan(store: Store, rules, ruff: RuffSetup):
    budget = CallBudget(store, "o/r", 1, "head", CallSettings(), now=lambda: NOW)
    return plan_and_author(
        rules, ruleset_key="k", store=store, budget=budget, models=ModelSettings(), ask=_no_model,
        sandbox=_NoSandbox(), ruff=ruff,
    )  # fmt: skip


def test_ruff_version_is_part_of_the_choice() -> None:
    assert tool_choice_pure.choice_key(UNUSED, "sonnet", "0.17.5") != tool_choice_pure.choice_key(
        UNUSED, "sonnet", "0.17.4"
    )


def test_tool_settings_rule_set_needs_no_model(store: Store) -> None:
    """도구 설정의 규칙 묶음은 결정적으로 정한다. 받을 수 없는 버전이면 검사하지 못한다."""
    rule = config_rule(RuffConfig("ruff.toml", "", "0" * 40))
    (check,) = _plan(store, (rule,), RuffSetup("0.17.5", config=None))
    assert isinstance(check, Checked) and tool_codes(check.checker.body) == ()
    (unavailable,) = _plan(store, (rule,), RuffSetup("0.1.0", available=False))
    assert isinstance(unavailable, Unchecked) and unavailable.kind == "tool_unavailable"


def test_prompt_lists_the_tools_and_keeps_the_rule_as_data() -> None:
    prompt = tool_choice_pure.build_prompt(UNUSED, tool_choice_pure.Shortfall(2, 2))
    assert "- ruff: " in prompt and "- ruff-format: " in prompt
    assert "<<<RULE id=" in prompt and "안 쓰는 import를 남기지 않는다." in prompt
    assert json.dumps(tool_choice_pure.ANSWER_SCHEMA["properties"]["tool"]) == json.dumps(
        {"enum": ["ruff", "ruff-format", "none"]}
    )
