"""app/models/checker_authoring.py: 검사 방법 계획, 캐시 키, 프롬프트, 예시 확인."""

from dataclasses import replace
from typing import Final

import pytest

from app.models.checker_authoring import (
    Answer,
    AnswerExample,
    Shortfall,
    Unchecked,
    build_prompt,
    checker_key,
    judge_examples,
    kind_for_attempt,
    model_examples,
    parse_answer,
    plan_check,
    shortfall,
)
from app.models.rule import CheckMethod, Example, Rule, RuleSource

BAD: Final = Example("def f(o):\n    print(o)\n", (2,), "document")
GOOD: Final = Example("logger.info('x')\n", (), "document")
RULE: Final = Rule(
    id="R-readme-1", text="`orders/` 아래 모듈은 `print`를 쓰지 않는다.",
    source=RuleSource("README.md", "코드 규칙 > 출력", "abc"), origin="document",
    scope=("orders/**",), check=CheckMethod("checker"), fix="model", examples=(BAD, GOOD),
)  # fmt: skip


def test_plan_skips_only_rules_that_code_cannot_show() -> None:
    for target in ("pr", "human"):
        other = replace(RULE, target=target, check=CheckMethod("none", reason="PR 크기"))
        assert plan_check(other) == Unchecked(other, target, "PR 크기")
    assert plan_check(RULE) is None
    assert plan_check(replace(RULE, check=CheckMethod("model", reason="뜻을 읽어야"))) is None


def test_format_switches_to_python_script_after_two_ast_grep_attempts() -> None:
    assert [kind_for_attempt(n, RULE) for n in (1, 2, 3)] == ["ast-grep", "ast-grep", "python-ast"]


def test_checker_key_follows_rule_text_examples_and_models_only() -> None:
    key = checker_key(RULE, "haiku")
    assert checker_key(replace(RULE, scope=()), "haiku") == key
    assert checker_key(replace(RULE, id="R-other"), "haiku") == key
    assert checker_key(replace(RULE, text=RULE.text + " 예외 없음."), "haiku") != key
    assert checker_key(replace(RULE, examples=(BAD,)), "haiku") != key
    assert checker_key(RULE, "sonnet") != key


@pytest.mark.parametrize(
    ("examples", "need"),
    [
        ((), Shortfall(2, 2)),
        ((BAD, GOOD), Shortfall(1, 1)),
        ((BAD, BAD, BAD, GOOD, GOOD), Shortfall(0, 0)),
    ],
)
def test_shortfall_counts_document_examples_first(examples: tuple, need: Shortfall) -> None:
    assert shortfall(examples) == need


def test_prompt_puts_rule_and_examples_in_delimited_data() -> None:
    prompt = build_prompt(RULE, Shortfall(1, 1), ["위반 예시 1 (문서): ..."])
    assert "<<<RULE id=" in prompt and RULE.text in prompt
    assert "적용 범위: orders/**" in prompt and "절: 코드 규칙 > 출력" in prompt
    assert "위반 예시, 어긴 줄 [2], 문서에 있음" in prompt
    assert "더 쓸 예시: 위반 예시 1개, 정상 예시 1개." in prompt
    assert "<<<FEEDBACK id=" in prompt and "위반 예시 1 (문서): ..." in prompt
    assert "<<<FEEDBACK id=" not in build_prompt(RULE, Shortfall(0, 0), [])


def test_parse_answer_strips_one_code_fence() -> None:
    answer = parse_answer({"checker": "```yaml\nid: x\nlanguage: python\n```", "examples": []})
    assert answer.checker == "id: x\nlanguage: python\n"
    with pytest.raises(ValueError):
        parse_answer({"examples": []})


def _answer(*examples: tuple[str, bool, list[int]]) -> Answer:
    return Answer(
        checker="x",
        examples=[AnswerExample(code=c, violates=v, lines=n) for c, v, n in examples],
    )


def test_model_examples_fill_the_shortfall() -> None:
    kept, problems = model_examples(
        (BAD, GOOD), _answer(("print(1)\n", True, [1]), ("log(1)\n", False, [5]))
    )
    assert problems == []
    assert kept == (
        Example("print(1)\n", (1,), "model"),
        Example("log(1)\n", (), "model"),
    )


@pytest.mark.parametrize(
    ("written", "problem"),
    [
        ((("print(1)\n", True, [1]),), "예시가 모자람"),
        ((("print(1)\n", True, []), ("log(1)\n", False, [])), "lines가 비어 있음"),
        ((("print(1)\n", True, [3]), ("log(1)\n", False, [])), "코드 밖"),
        ((("def (:\n", True, [1]), ("log(1)\n", False, [])), "문법 오류"),
        (((BAD.code, True, [2]), ("log(1)\n", False, [])), "같은 코드"),
    ],
)
def test_model_example_problems(written: tuple, problem: str) -> None:
    _, problems = model_examples((BAD, GOOD), _answer(*written))
    assert any(problem in p for p in problems), problems


def test_judge_passes_when_violations_are_on_the_marked_lines() -> None:
    assert judge_examples((BAD, GOOD), [[(2, 2)], []]) == []
    assert judge_examples((BAD,), [[(1, 2)]]) == []  # 여러 줄에 걸친 노드가 표시한 줄을 덮음


@pytest.mark.parametrize(
    ("spans", "says"),
    [
        ([[], []], "위반 예시 1 (문서): 2번째 줄에서 잡혀야 함, 실제로는 잡히지 않음."),
        ([[(1, 1)], []], "위반 예시 1 (문서): 2번째 줄에서 잡혀야 함, 실제로는 1번째 줄에서 잡힘."),
        ([[(2, 2)], [(1, 1)]], "정상 예시 1 (문서): 잡히면 안 됨, 실제로는 1번째 줄에서 잡힘."),
    ],
)
def test_judge_names_the_example_and_expected_vs_reported(spans: list, says: str) -> None:
    feedback = judge_examples((BAD, GOOD), spans)
    assert len(feedback) == 1 and feedback[0].startswith(says), feedback
    assert "  1| " in feedback[0]  # 줄 번호를 붙인 코드


def test_judge_labels_model_examples() -> None:
    model_bad = Example("print(2)\n", (1,), "model")
    feedback = judge_examples((BAD, model_bad), [[(2, 2)], []])
    assert feedback[0].startswith("위반 예시 2 (모델이 씀)")


def test_prompt_version_follows_prompt_text() -> None:
    from app.models import checker_authoring
    from app.models.rule_reading import prompt_version

    changed = prompt_version("다른 지시", checker_authoring.ANSWER_SCHEMA, "")
    assert len(checker_authoring.PROMPT_VERSION) == 12
    assert checker_authoring.PROMPT_VERSION != changed


PATH_RULE: Final = replace(RULE, check=CheckMethod("path"), examples=(), scope=())


def test_path_rule_uses_only_the_path_pattern_kind() -> None:
    assert [kind_for_attempt(n, PATH_RULE) for n in (1, 2, 3)] == ["path-pattern"] * 3
    as_code = replace(PATH_RULE, check=CheckMethod("checker"))
    assert checker_key(PATH_RULE, "haiku") != checker_key(as_code, "haiku")


def test_path_examples_are_paths_and_violations_are_per_file() -> None:
    answer = Answer(
        checker="{}",
        examples=[
            AnswerExample(code="tests/pricing_test.py", violates=True),
            AnswerExample(code=" tests/checks.py\n", violates=True, lines=[3]),
            AnswerExample(code="tests/test_pricing.py", violates=False),
            AnswerExample(code="tests/__init__.py", violates=False),
        ],
    )
    examples, problems = model_examples((), answer, "path-pattern")
    assert problems == []
    assert [(e.code, e.expect_lines) for e in examples] == [
        ("tests/pricing_test.py", (1,)), ("tests/checks.py", (1,)),
        ("tests/test_pricing.py", ()), ("tests/__init__.py", ()),
    ]  # fmt: skip


@pytest.mark.parametrize("path", ["../etc/passwd", "/abs/x.py", "a.py b.py", "", "x" * 201])
def test_path_example_outside_the_repo_shape_is_not_used(path: str) -> None:
    answer = Answer(checker="{}", examples=[AnswerExample(code=path, violates=True)])
    examples, problems = model_examples((), answer, "path-pattern")
    assert examples == ()
    assert problems
