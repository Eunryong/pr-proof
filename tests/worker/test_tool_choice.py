"""worker/checkers.py의 도구 고르기 (docs/DESIGN.md 4.4 (a)): 문서 규칙에 저장소의 도구(ruff)를
잇고, 도구 설정의 규칙 묶음은 모델 없이 ruff로 정한다. 가짜 모델과 가짜 샌드박스, 실제 SQLite."""

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import fakeredis
import pytest

from app.models import tool_choice
from app.models.checker import CheckerKind, tool_codes
from app.models.checker_authoring import Checked, Unchecked
from app.models.repo_config import CallSettings, ModelSettings
from app.models.rule import CheckMethod, Rule, RuleSource
from app.models.tools import RuffConfig, RuffSetup, config_rule
from app.sandbox import checker_runner, toolcache
from app.sandbox.checker_runner import CheckerRun, Match, Ran
from app.worker.ai.claude_cli import StructuredResult
from app.worker.budget import CallBudget
from app.worker.checkers import plan_and_author
from app.worker.state import Store

NOW: Final = datetime(2026, 11, 20, 12, tzinfo=UTC)
RUFF: Final = RuffSetup("0.17.5")
UNUSED: Final = Rule(
    id="R-readme-2", text="안 쓰는 import를 남기지 않는다.",
    source=RuleSource("README.md", "코드 규칙 > import", "abc"), origin="document",
    scope=(), check=CheckMethod("checker"), fix="model",
)  # fmt: skip
EXAMPLES: Final = [
    {"code": "import os\n", "violates": True, "lines": [1]},
    {"code": "import os, sys\nprint(sys)\n", "violates": True, "lines": [1]},
    {"code": "import os\nprint(os)\n", "violates": False, "lines": []},
    {"code": "x = 1\n", "violates": False, "lines": []},
]


class FakeRuffSandbox:
    """`import os`가 든 줄이 쓰이지 않으면 F401로 잡는 가짜 ruff. 아는 코드는 known뿐이다."""

    def __init__(self, known: Sequence[str] = ("F401", "T201")) -> None:
        self.known = set(known)
        self.runs: list[tuple[CheckerKind, str]] = []

    def on_examples(self, kind: CheckerKind, body: str, snippets: Sequence[str]) -> CheckerRun:
        self.runs.append((kind, body))
        assert kind == "ruff" and tool_codes(body) == ("F401",)
        return Ran(tuple(
            Match(checker_runner.example_path(i), n, n, "unused", "F401")
            for i, code in enumerate(snippets)
            for n, line in enumerate(code.splitlines(), 1)
            if "import os" in line and "os)" not in code
        ))  # fmt: skip

    def on_files(self, kind, body, root, files) -> CheckerRun:
        raise AssertionError("도구 고르기는 저장소 파일을 돌리지 않는다")

    def rule_exists(self, code: str) -> bool:
        return code in self.known


def _answer(
    tool: str = "ruff", codes: Sequence[str] = ("F401",), examples=None
) -> StructuredResult:
    data = {"tool": tool, "codes": list(codes), "reason": "F401이 같은 것을 잡는다",
            "examples": EXAMPLES if examples is None else examples}  # fmt: skip
    return StructuredResult(ok=True, error=None, data=data, model_id="claude-sonnet-5",
                            input_tokens=500, output_tokens=50, cost_usd=None,
                            permission_denials=0)  # fmt: skip


class FakeModel:
    """도구 선택에는 tool의 답을, 검사기 작성에는 checker의 답을 준다."""

    def __init__(self, tool: StructuredResult, checker: StructuredResult | None = None) -> None:
        self.tool, self.checker = tool, checker
        self.asked: list[str] = []

    def __call__(self, prompt: str, *, schema, **kw) -> StructuredResult:
        if schema is tool_choice.ANSWER_SCHEMA:
            self.asked.append("tool")
            return self.tool
        self.asked.append("checker")
        assert self.checker is not None, "검사기 작성까지 가면 안 된다"
        return self.checker


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "pipeline.db", fakeredis.FakeStrictRedis())


def _plan(store: Store, model: FakeModel, sandbox=None, rules=(UNUSED,), ruff=RUFF):
    budget = CallBudget(store, "o/r", 1, "head", CallSettings(), now=lambda: NOW)
    return plan_and_author(
        rules, ruleset_key="k", store=store, budget=budget, models=ModelSettings(), ask=model,
        sandbox=sandbox or FakeRuffSandbox(), ruff=ruff,
    )  # fmt: skip


def test_rule_that_a_ruff_code_checks_is_checked_by_that_code(store: Store) -> None:
    """3-2: "안 쓰는 import를 남기지 않는다"는 ruff F401로 이어진다. 규칙은 문서 규칙 그대로라
    출처가 README다. 고른 코드는 `ruff rule`로 있는지 보고, 예시로 확인해 채택한다."""
    model = FakeModel(_answer())
    (check,) = _plan(store, model)
    assert isinstance(check, Checked) and check.rule.source.file == "README.md"
    assert (check.checker.kind, tool_codes(check.checker.body)) == ("ruff", ("F401",))
    assert check.checker.engine_version == "ruff-0.17.5" and len(check.checker.examples) == 4
    assert model.asked == ["tool"]
    (again,) = _plan(store, FakeModel(_answer(tool="none")))  # 저장된 선택을 쓴다
    assert isinstance(again, Checked) and again.cached


@pytest.mark.parametrize(
    ("answer", "why"),
    [
        (_answer(tool="none"), "맞는 도구가 없음"),
        (_answer(codes=("F999",)), "없는 규칙 코드: F999"),
        (_answer(codes=("F",)), "형식이 틀림"),
        (_answer(examples=[*EXAMPLES, {"code": "import os  # x\n", "violates": False,
                                        "lines": []}]), "틀리게 가림"),
    ],
    ids=["none", "unknown-code", "prefix", "wrong-examples"],
)  # fmt: skip
def test_unfit_tool_choice_falls_through_to_checker_authoring(
    store: Store, answer: StructuredResult, why: str
) -> None:
    """도구가 맞지 않거나 확인을 통과하지 못하면 검사기 작성으로 넘어간다 (4.4). 그 결정도 저장해
    다음 검사는 다시 묻지 않는다."""
    checker = StructuredResult(ok=False, error=None, data=None, model_id="", input_tokens=None,
                               output_tokens=None, cost_usd=None, permission_denials=0,
                               detail="여기서 멈춘다")  # fmt: skip
    model = FakeModel(answer, checker)
    (check,) = _plan(store, model)
    assert model.asked == ["tool", "checker"] and isinstance(check, Unchecked)
    saved = store.saved_checker(tool_choice.choice_key(UNUSED, "sonnet", "0.17.5"))
    assert saved is not None and not saved.accepted and why in saved.detail
    model = FakeModel(answer, checker)
    _plan(store, model)
    assert model.asked == ["checker"]


def test_tool_list_is_not_offered_when_the_pinned_ruff_cannot_be_fetched(store: Store) -> None:
    """저장소가 고정한 ruff를 파이프라인이 받을 수 없으면 도구를 고르게 하지 않는다."""
    checker = StructuredResult(ok=False, error=None, data=None, model_id="", input_tokens=None,
                               output_tokens=None, cost_usd=None, permission_denials=0)  # fmt: skip
    model = FakeModel(_answer(), checker)
    _plan(store, model, ruff=RuffSetup("0.1.0", "requirements.txt", available=False))
    assert model.asked == ["checker"]


def test_ruff_version_is_part_of_the_choice(store: Store) -> None:
    assert tool_choice.choice_key(UNUSED, "sonnet", "0.17.5") != tool_choice.choice_key(
        UNUSED, "sonnet", "0.17.4"
    )


def test_tool_settings_rule_set_needs_no_model(store: Store) -> None:
    """도구 설정의 규칙 묶음은 결정적으로 정한다. 받을 수 없는 버전이면 검사하지 못한다."""
    rule = config_rule(RuffConfig("ruff.toml", "", "0" * 40))
    model = FakeModel(_answer())
    (check,) = _plan(store, model, rules=(rule,), ruff=RuffSetup("0.17.5", config=None))
    assert isinstance(check, Checked) and tool_codes(check.checker.body) == ()
    assert model.asked == []
    (unavailable,) = _plan(store, model, rules=(rule,), ruff=RuffSetup("0.1.0", available=False))
    assert isinstance(unavailable, Unchecked) and unavailable.kind == "tool_unavailable"


def test_tool_that_cannot_be_fetched_leaves_the_rule_for_the_next_check(store: Store) -> None:
    class Unavailable(FakeRuffSandbox):
        def rule_exists(self, code: str) -> bool:
            raise toolcache.ToolUnavailable("해시 불일치")

    (check,) = _plan(store, FakeModel(_answer()), sandbox=Unavailable())
    assert isinstance(check, Unchecked) and check.kind == "tool_unavailable"
    assert store.saved_checker(tool_choice.choice_key(UNUSED, "sonnet", "0.17.5")) is None


def test_prompt_lists_the_tools_and_keeps_the_rule_as_data() -> None:
    prompt = tool_choice.build_prompt(UNUSED, tool_choice.Shortfall(2, 2))
    assert "- ruff: " in prompt and "- ruff-format: " in prompt
    assert "<<<RULE id=" in prompt and "안 쓰는 import를 남기지 않는다." in prompt
    assert json.dumps(tool_choice.ANSWER_SCHEMA["properties"]["tool"]) == json.dumps(
        {"enum": ["ruff", "ruff-format", "none"]}
    )
