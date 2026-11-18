"""worker/checkers.py: 검사기 작성 루프. 가짜 모델과 가짜 샌드박스, 실제 SQLite로 확인한다."""

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import fakeredis
import pytest

from app.models.checker import CheckerKind
from app.models.checker_authoring import SYSTEM_PROMPT, Checked, Unchecked
from app.models.repo_config import CallSettings, ModelSettings
from app.models.rule import CheckMethod, Example, Rule, RuleSource
from app.sandbox import checker_runner, toolcache
from app.sandbox.checker_runner import CheckerRun, Invalid, Match, Ran
from app.worker.ai.claude_cli import AIErrorKind, StructuredResult
from app.worker.budget import CallBudget
from app.worker.checkers import plan_and_author
from app.worker.state import Store

RULESET: Final = "ruleset-key"
MODELS: Final = ModelSettings(light="haiku", heavy="sonnet")
NOW: Final = datetime(2026, 11, 18, 12, tzinfo=UTC)
BAD: Final = Example("def f(o):\n    print(o)\n", (2,), "document")
GOOD: Final = Example("logger.info('x')\n", (), "document")
RULE: Final = Rule(
    id="R-readme-1", text="`orders/` 아래 모듈은 `print`를 쓰지 않는다.",
    source=RuleSource("README.md", "코드 규칙 > 출력", "abc"), origin="document",
    scope=("orders/**",), check=CheckMethod("checker"), fix="model", examples=(BAD, GOOD),
)  # fmt: skip
MODEL_EXAMPLES: Final = [
    {"code": "print(1)\n", "violates": True, "lines": [1]},
    {"code": "log(1)\n", "violates": False, "lines": []},
]


class GrepSandbox:
    """본문이 `grep:<글>`이면 그 글이 든 줄을 잡는 가짜 검사기. `invalid`면 엔진 오류."""

    def __init__(self, error: Exception | None = None) -> None:
        self.runs: list[tuple[CheckerKind, str, list[str]]] = []
        self.error = error

    def on_examples(self, kind: CheckerKind, body: str, snippets: Sequence[str]) -> CheckerRun:
        self.runs.append((kind, body, list(snippets)))
        if self.error is not None:
            raise self.error
        if body.strip() == "invalid":
            return Invalid("Error: Cannot parse rule")
        needle = body.strip().removeprefix("grep:")
        return Ran(
            tuple(
                Match(checker_runner.example_path(i), n, n, "m")
                for i, code in enumerate(snippets)
                for n, line in enumerate(code.splitlines(), 1)
                if needle in line
            )
        )

    def on_files(
        self, kind: CheckerKind, body: str, root: Path, files: Sequence[str]
    ) -> CheckerRun:
        raise AssertionError("작성 단계는 저장소 파일을 돌리지 않는다")


def _reply(
    checker: str = "grep:print(", examples: list | None = None, **fields: object
) -> StructuredResult:
    values = {
        "ok": True, "error": None,
        "data": {"checker": checker, "examples": MODEL_EXAMPLES if examples is None else examples},
        "model_id": "claude-haiku-5-5", "input_tokens": 1000, "output_tokens": 100,
        "cost_usd": 0.001, "permission_denials": 0,
    }  # fmt: skip
    values.update(fields)
    return StructuredResult(**values)


class FakeModel:
    def __init__(self, *answers: StructuredResult) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, prompt: str, **kwargs: object) -> StructuredResult:
        self.calls.append((prompt, kwargs))
        return self.answers.pop(0)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "pipeline.db", fakeredis.FakeStrictRedis())


def _budget(store: Store, **settings: int) -> CallBudget:
    return CallBudget(store, "o/r", 1, "head", CallSettings(**settings), now=lambda: NOW)


def _author(store: Store, model: FakeModel, sandbox: GrepSandbox | None = None,
            rules: Sequence[Rule] = (RULE,), **settings: int):  # fmt: skip
    return plan_and_author(
        rules, ruleset_key=RULESET, store=store, budget=_budget(store, **settings),
        models=MODELS, ask=model, sandbox=sandbox or GrepSandbox(),
    )  # fmt: skip


def _counted(store: Store) -> list[tuple[str, int, bool]]:
    return [(c.tier, c.counted, c.cache_hit) for c in store.model_calls("o/r", 1)]


def test_accepts_checker_that_passes_document_and_model_examples(store: Store) -> None:
    model, sandbox = FakeModel(_reply()), GrepSandbox()
    (result,) = _author(store, model, sandbox)
    assert isinstance(result, Checked) and not result.cached
    checker = result.checker
    assert checker.kind == "ast-grep" and checker.body == "grep:print(\n"
    assert [e.written_by for e in checker.examples] == ["document", "document", "model", "model"]
    assert checker.model == "claude-haiku-5-5" and checker.engine_version == "ast-grep-0.51.3"
    assert sandbox.runs[0][2] == [BAD.code, GOOD.code, "print(1)\n", "log(1)\n"]
    _, kwargs = model.calls[0]
    assert kwargs["model"] == "haiku" and kwargs["system_prompt"] == SYSTEM_PROMPT["ast-grep"]
    assert _counted(store) == [("light", 1, False)]


def test_second_run_reuses_the_checker_without_model_calls(store: Store) -> None:
    (first,) = _author(store, FakeModel(_reply()))
    (second,) = _author(store, FakeModel())
    assert isinstance(second, Checked) and second.cached
    assert second.checker.body == first.checker.body
    assert _counted(store) == [("light", 1, False), ("light", 0, True)]


def test_wrong_checker_is_retried_with_the_failed_example(store: Store) -> None:
    model = FakeModel(_reply(checker="grep:pprint("), _reply())
    (result,) = _author(store, model)
    assert isinstance(result, Checked)
    retry_prompt = model.calls[1][0]
    assert "위반 예시 1 (문서): 2번째 줄에서 잡혀야 함, 실제로는 잡히지 않음." in retry_prompt
    assert "직전 검사기 (ast-grep):\ngrep:pprint(" in retry_prompt
    assert [c[1] for c in _counted(store)] == [1, 1]


def test_checker_flagging_a_good_example_is_retried(store: Store) -> None:
    model = FakeModel(_reply(checker="grep:("), _reply())
    (result,) = _author(store, model)
    assert isinstance(result, Checked)
    assert "정상 예시 1 (문서): 잡히면 안 됨, 실제로는 1번째 줄에서 잡힘." in model.calls[1][0]


def test_exhausted_attempts_end_as_not_yet_checkable_and_are_remembered(store: Store) -> None:
    wrong = _reply(checker="grep:nothing")
    model = FakeModel(wrong, wrong, wrong)
    (result,) = _author(store, model)
    assert isinstance(result, Unchecked) and result.kind == "no_checker"
    assert "3회 안에 예시를 통과하지 못함" in result.detail
    third = model.calls[2][1]
    assert third["model"] == "sonnet" and third["system_prompt"] == SYSTEM_PROMPT["python-ast"]
    assert [c[:2] for c in _counted(store)] == [("light", 1), ("light", 1), ("heavy", 3)]
    (again,) = _author(store, FakeModel())
    assert isinstance(again, Unchecked) and again.kind == "no_checker"
    assert _counted(store)[-1] == ("light", 0, True)


def test_engine_error_is_fed_back(store: Store) -> None:
    model = FakeModel(_reply(checker="invalid"), _reply())
    (result,) = _author(store, model)
    assert isinstance(result, Checked)
    assert "검사기가 틀림:\nError: Cannot parse rule" in model.calls[1][0]


def test_bad_model_examples_are_fed_back(store: Store) -> None:
    model = FakeModel(_reply(examples=[]), _reply())
    (result,) = _author(store, model)
    assert isinstance(result, Checked)
    assert "예시가 모자람" in model.calls[1][0]


def test_malformed_answer_counts_as_an_attempt(store: Store) -> None:
    model = FakeModel(_reply(data={"examples": []}), _reply())
    (result,) = _author(store, model)
    assert isinstance(result, Checked)
    assert "답의 형식이 틀림" in model.calls[1][0]


def test_model_call_failure_is_not_saved(store: Store) -> None:
    failed = _reply(ok=False, error=AIErrorKind.TIMEOUT, data=None, detail="600초")
    (result,) = _author(store, FakeModel(failed))
    assert isinstance(result, Unchecked) and result.kind == "model_failed"
    assert _counted(store) == [("light", 1, False)]
    (retried,) = _author(store, FakeModel(_reply()))
    assert isinstance(retried, Checked)


def test_cap_stops_authoring_and_next_check_resumes(store: Store) -> None:
    second = replace(RULE, id="R-readme-2", text="`global` 문을 쓰지 않는다.")
    # 규칙 묶음 상한 1: 첫 규칙의 첫 회차만 부를 수 있다
    wrong = _reply(checker="grep:nothing")
    model = FakeModel(wrong)
    first, other = _author(store, model, rules=(RULE, second), ruleset_base=1, per_rule=0)
    assert isinstance(first, Unchecked) and first.kind == "cap" and "규칙 묶음 상한" in first.detail
    assert isinstance(other, Unchecked) and other.kind == "cap"
    assert len(model.calls) == 1
    # 상한이 늘면 저장한 1회차 답을 다시 쓰고(0회) 2회차부터 잇는다
    model = FakeModel(_reply(), _reply())
    first, other = _author(store, model, rules=(RULE, second))
    assert isinstance(first, Checked) and isinstance(other, Checked)
    assert "위반 예시 1 (문서)" in model.calls[0][0]  # 2회차 프롬프트에 1회차 실패가 있다
    assert [c[1:] for c in _counted(store)] == [(1, False), (0, True), (1, False), (1, False)]


def test_rules_code_cannot_show_get_no_calls(store: Store) -> None:
    none_rule = replace(RULE, check=CheckMethod("none", reason="PR 크기 규칙"))
    model = FakeModel()
    (result,) = _author(store, model, rules=(none_rule,))
    assert result == Unchecked(none_rule, "not_code", "PR 크기 규칙")
    assert model.calls == [] and _counted(store) == []


def test_tool_unavailable_marks_rule_and_is_retried_next_time(store: Store) -> None:
    sandbox = GrepSandbox(error=toolcache.ToolUnavailable("해시 불일치"))
    (result,) = _author(store, FakeModel(_reply()), sandbox)
    assert isinstance(result, Unchecked) and result.kind == "tool_unavailable"
    (retried,) = _author(store, FakeModel(), GrepSandbox())
    assert isinstance(retried, Checked)  # 저장한 1회차 답을 다시 확인해 채택한다
