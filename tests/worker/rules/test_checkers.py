"""app/worker/rules/checkers.py: 검사기 작성 루프에서 모델을 부르지 않는 길. 실제 SQLite로
확인한다."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest

from app.worker.budget import CallBudget
from app.worker.checker_authoring_pure import Unchecked
from app.worker.config.repo_config_pure import CallSettings, ModelSettings
from app.worker.core.rule_pure import CheckMethod, Example, Rule, RuleSource
from app.worker.rules.checkers import plan_and_author
from app.worker.state import Store

from ...redis_stand_ins import MemoryKeys

RULESET: Final = "ruleset-key"
MODELS: Final = ModelSettings(checker="sonnet", heavy="opus")
NOW: Final = datetime(2026, 11, 18, 12, tzinfo=UTC)
BAD: Final = Example("def f(o):\n    print(o)\n", (2,), "document")
GOOD: Final = Example("logger.info('x')\n", (), "document")
RULE: Final = Rule(
    id="R-readme-1", text="`orders/` 아래 모듈은 `print`를 쓰지 않는다.",
    source=RuleSource("README.md", "코드 규칙 > 출력", "abc"), origin="document",
    scope=("orders/**",), check=CheckMethod("checker"), fix="model", examples=(BAD, GOOD),
)  # fmt: skip


def _no_model(prompt: str, **kwargs: object):
    pytest.fail("모델을 부르지 않아야 한다")


class _NoSandbox:
    def on_examples(self, *args: object):
        raise AssertionError("검사기를 돌리지 않아야 한다")

    def on_files(self, *args: object):
        raise AssertionError("검사기를 돌리지 않아야 한다")


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "pipeline.db", MemoryKeys())


def test_rules_code_cannot_show_get_no_calls(store: Store) -> None:
    none_rule = replace(RULE, target="pr", check=CheckMethod("none", reason="PR 크기 규칙"))
    budget = CallBudget(store, "o/r", 1, "head", CallSettings(), now=lambda: NOW)
    (result,) = plan_and_author(
        (none_rule,), ruleset_key=RULESET, store=store, budget=budget, models=MODELS,
        ask=_no_model, sandbox=_NoSandbox(),
    )  # fmt: skip
    assert result == Unchecked(none_rule, "pr", "PR 크기 규칙")
    assert store.model_calls("o/r", 1) == []
