"""worker/checkers.py: 검사기 작성 루프. 가짜 모델과 가짜 샌드박스, 실제 SQLite로 확인한다."""

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import fakeredis
import pytest

from app.models.checker import CheckerKind
from app.models.checker_authoring import SYSTEM_PROMPT, Checked, ToJudge, Unchecked, checker_key
from app.models.repo_config import CallSettings, ModelSettings
from app.models.rule import CheckMethod, Example, Rule, RuleSource
from app.sandbox import checker_runner, toolcache
from app.sandbox.checker_runner import CheckerRun, Invalid, Match, Ran
from app.worker.ai.claude_cli import AIErrorKind, StructuredResult
from app.worker.budget import CallBudget
from app.worker.checkers import plan_and_author
from app.worker.state import Store

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
    checker: str = "grep:print(", examples: list | None = None, unfit: str = "",
    coverage: str = "all", uncovered: str = "", **fields: object,
) -> StructuredResult:  # fmt: skip
    values = {
        "ok": True, "error": None,
        "data": {"checker": checker, "examples": MODEL_EXAMPLES if examples is None else examples,
                 "unfit": unfit, "coverage": coverage, "uncovered": uncovered},  # fmt: skip
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
    assert kwargs["model"] == "sonnet" and kwargs["system_prompt"] == SYSTEM_PROMPT["ast-grep"]
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


def test_second_failed_example_check_sends_the_rule_to_judgment_and_is_remembered(
    store: Store,
) -> None:
    wrong = _reply(checker="grep:nothing")
    model = FakeModel(wrong, wrong)
    (result,) = _author(store, model)
    assert isinstance(result, ToJudge) and result.route == "examples"
    assert result.detail.startswith("예시 확인을 2번 통과하지 못함: 예시 2개를 틀리게 가림")
    # 세 번째 회차는 없다. 모델은 올리지 않는다 (11.2 D7)
    assert [call[1]["model"] for call in model.calls] == ["sonnet"] * 2
    assert [c[:2] for c in _counted(store)] == [("light", 1), ("light", 1)]
    saved = store.saved_checker(checker_key(RULE, MODELS.checker))
    assert saved is not None and not saved.accepted and saved.judge_route == "examples"
    (again,) = _author(store, FakeModel())  # 다시 쓰지 않는다
    assert again == result
    assert _counted(store)[-1] == ("light", 0, True)


def test_attempts_used_up_without_two_example_failures_still_go_to_judgment(store: Store) -> None:
    malformed = _reply(data={"examples": []})
    model = FakeModel(malformed, _reply(checker="grep:nothing"), malformed)
    (result,) = _author(store, model)
    assert isinstance(result, ToJudge) and result.route == "attempts"
    assert "3회 안에 예시를 통과하지 못함" in result.detail
    assert model.calls[2][1]["system_prompt"] == SYSTEM_PROMPT["python-ast"]


def test_path_rule_runs_all_three_attempts_and_never_goes_to_judgment(store: Store) -> None:
    path_rule = replace(RULE, check=CheckMethod("path"), examples=(), scope=())
    wrong = _reply(checker="grep:nothing", examples=[
        {"code": "orders/a.py", "violates": True, "lines": []},
        {"code": "orders/b.py", "violates": True, "lines": []},
        {"code": "tests/test_a.py", "violates": False, "lines": []},
        {"code": "tests/test_b.py", "violates": False, "lines": []},
    ], coverage="none", uncovered="무시되어야 한다")  # fmt: skip
    model = FakeModel(wrong, wrong, wrong)
    (result,) = _author(store, model, rules=(path_rule,))
    assert isinstance(result, Unchecked) and result.kind == "no_checker"
    assert "3회 안에 예시를 통과하지 못함" in result.detail
    assert all("coverage" not in c[1]["schema"]["properties"] for c in model.calls)
    saved = store.saved_checker(checker_key(path_rule, MODELS.checker))
    assert saved is not None and saved.judge_route is None
    (again,) = _author(store, FakeModel(), rules=(path_rule,))
    assert isinstance(again, Unchecked) and again.kind == "no_checker"


@pytest.mark.parametrize("coverage", ["none", "part"])
def test_none_or_part_answer_goes_to_judgment_without_running_the_checker(
    store: Store, coverage: str
) -> None:
    # "일부"라고 답한 검사기는 예시를 통과할 수 있어도 돌리지 않고 채택하지 않는다
    answer = _reply(checker="grep:print(", coverage=coverage, uncovered="주석의 뜻을 읽어야 한다")
    model, sandbox = FakeModel(answer), GrepSandbox()
    (result,) = _author(store, model, sandbox)
    assert result == ToJudge(RULE, coverage, "주석의 뜻을 읽어야 한다")
    assert sandbox.runs == []
    assert [c[:2] for c in _counted(store)] == [("light", 1)]
    saved = store.saved_checker(checker_key(RULE, MODELS.checker))
    assert saved is not None and saved.judge_route == coverage and saved.checker_json is None
    (again,) = _author(store, FakeModel(), GrepSandbox())
    assert again == result


def test_part_answer_on_the_python_attempt_after_unfit_goes_to_judgment(store: Store) -> None:
    model = FakeModel(_reply(unfit="스코프를 따져야 한다"),
                      _reply(coverage="part", uncovered="뜻을 읽어야 하는 모양"))  # fmt: skip
    (result,) = _author(store, model)
    assert isinstance(result, ToJudge) and result.route == "part"
    assert model.calls[1][1]["system_prompt"] == SYSTEM_PROMPT["python-ast"]


def test_one_failed_example_check_then_none_goes_to_judgment_as_none(store: Store) -> None:
    model = FakeModel(_reply(checker="grep:nothing"), _reply(coverage="none", uncovered="뜻"))
    (result,) = _author(store, model)
    assert isinstance(result, ToJudge) and result.route == "none"


def test_cap_before_judgment_routing_is_not_saved_and_is_retried(store: Store) -> None:
    wrong = _reply(checker="grep:nothing")
    (first,) = _author(store, FakeModel(wrong), ruleset_base=1, per_rule=0)
    assert isinstance(first, Unchecked) and first.kind == "cap"
    assert store.saved_checker(checker_key(RULE, MODELS.checker)) is None
    # 다음 검사는 저장한 1회차를 다시 쓰고 2회차의 두 번째 실패로 모델 판단에 보낸다
    (second,) = _author(store, FakeModel(wrong))
    assert isinstance(second, ToJudge) and second.route == "examples"


def test_ast_grep_unfit_answer_switches_to_a_python_script_on_the_next_attempt(
    store: Store,
) -> None:
    unfit = _reply(checker="grep:print(", unfit="이름이 정의된 곳을 따져야 한다")
    model, sandbox = FakeModel(unfit, _reply()), GrepSandbox()
    (result,) = _author(store, model, sandbox)
    # 2-1의 02가 잡히는 길: unfit(형식의 한계)은 모델 판단으로 보내지 않는다
    assert isinstance(result, Checked) and result.checker.kind == "python-ast"
    # unfit이라고 답한 ast-grep 초안은 예시를 통과할 수 있어도 돌리지 않는다
    assert [run[0] for run in sandbox.runs] == ["python-ast"]
    first, second = (call[1] for call in model.calls)
    assert first["system_prompt"] == SYSTEM_PROMPT["ast-grep"]
    assert "unfit" in first["schema"]["required"]
    assert second["system_prompt"] == SYSTEM_PROMPT["python-ast"]
    assert "unfit" not in second["schema"]["required"]
    assert "ast-grep으로 가릴 수 없다고 답했다: 이름이 정의된 곳을 따져야 한다" in model.calls[1][0]
    assert [call[1]["model"] for call in model.calls] == ["sonnet", "sonnet"]
    assert [c[:2] for c in _counted(store)] == [("light", 1), ("light", 1)]


def test_unfit_does_not_count_as_a_failed_example_check(store: Store) -> None:
    wrong = _reply(checker="grep:nothing")
    model = FakeModel(_reply(unfit="정의된 곳을 따져야 한다"), wrong, wrong)
    (result,) = _author(store, model)
    assert isinstance(result, ToJudge) and result.route == "examples"
    assert [c[1]["system_prompt"] for c in model.calls] == [
        SYSTEM_PROMPT["ast-grep"], SYSTEM_PROMPT["python-ast"], SYSTEM_PROMPT["python-ast"],
    ]  # fmt: skip
    assert [call[1]["model"] for call in model.calls] == ["sonnet"] * 3


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


def test_model_call_failure_after_one_failed_example_check_is_retried(store: Store) -> None:
    failed = _reply(ok=False, error=AIErrorKind.TIMEOUT, data=None, detail="600초")
    model = FakeModel(_reply(checker="grep:nothing"), failed)
    (result,) = _author(store, model)
    assert isinstance(result, Unchecked) and result.kind == "model_failed"
    assert store.saved_checker(checker_key(RULE, MODELS.checker)) is None
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
    none_rule = replace(RULE, target="pr", check=CheckMethod("none", reason="PR 크기 규칙"))
    model = FakeModel()
    (result,) = _author(store, model, rules=(none_rule,))
    assert result == Unchecked(none_rule, "pr", "PR 크기 규칙")
    assert model.calls == [] and _counted(store) == []


def test_tool_unavailable_marks_rule_and_is_retried_next_time(store: Store) -> None:
    sandbox = GrepSandbox(error=toolcache.ToolUnavailable("해시 불일치"))
    (result,) = _author(store, FakeModel(_reply()), sandbox)
    assert isinstance(result, Unchecked) and result.kind == "tool_unavailable"
    (retried,) = _author(store, FakeModel(), GrepSandbox())
    assert isinstance(retried, Checked)  # 저장한 1회차 답을 다시 확인해 채택한다


# --- 검사기 기록 (4.10 표의 마지막 줄)과 검색 (6.2) ---


def test_accepted_checker_is_kept_as_a_checker_record(store: Store) -> None:
    (result,) = _author(store, FakeModel(_reply()))
    (record,) = store.records("o/r")
    assert (record.kind, record.origin, record.rule_id) == ("checker", "ruleset", RULE.id)
    assert record.rule_text == RULE.text and record.file == "" and record.context == ""
    assert record.diff.startswith("# 형식: ast-grep\ngrep:print(")
    assert "# 위반 예시, 어긴 줄 [2]" in record.diff and "# 정상 예시" in record.diff
    _author(store, FakeModel())  # 저장한 검사기를 다시 쓰면 기록이 늘지 않는다
    assert len(store.records("o/r")) == 1


def test_checker_record_with_a_secret_is_not_kept(store: Store) -> None:
    secret = "ghp_" + "a" * 36
    examples = [MODEL_EXAMPLES[0], {"code": f"x = '{secret}'\n", "violates": False, "lines": []}]
    (result,) = _author(store, FakeModel(_reply(examples=examples)))
    assert isinstance(result, Checked)
    assert store.records("o/r") == []


class _Found:
    """정해 둔 기록을 돌려주는 가짜 검색. 받은 질의를 남긴다."""

    def __init__(self, selection) -> None:
        self.selection, self.error = selection, ""
        self.queries: list[tuple[str, list[str]]] = []

    def find(self, site: str, queries):
        self.queries.append((site, list(queries)))
        return self.selection


def test_similar_checker_records_go_into_every_attempt_and_the_draft_key(store: Store) -> None:
    from app.models.checker_authoring import RECORDS_NOTE
    from app.models.db import Record
    from app.models.rag import Chosen, Retrieval, Selection

    old = Record(id=7, repo="o/r", kind="checker", origin="ruleset", source_id="c", rule_id="R-x",
                 rule_text="서비스는 logging만 쓴다", file="", line_text="", context="",
                 diff="# 형식: ast-grep\nrule: x", created_at=NOW)  # fmt: skip
    found = _Found(Selection((Chosen(old, 0.93),), (8,)))
    seen: list[Retrieval] = []
    model = FakeModel(_reply(checker="grep:pprint("), _reply())
    (result,) = plan_and_author(
        (RULE,), ruleset_key=RULESET, store=store, budget=_budget(store), models=MODELS,
        ask=model, sandbox=GrepSandbox(), retriever=found, on_retrieval=seen.append,
    )  # fmt: skip
    assert isinstance(result, Checked)
    assert found.queries == [("checker", ["규칙: " + RULE.text])]  # 규칙마다 한 번
    for prompt, kwargs in model.calls:
        assert kwargs["system_prompt"] == SYSTEM_PROMPT["ast-grep"] + RECORDS_NOTE
        assert "<<<RECORDS id=" in prompt and "기록 7 · 검사기 · 규칙 R-x" in prompt
    (r,) = seen
    assert [(x.record_id, x.site) for x in r.references] == [(7, "checker")] and r.below == 1
