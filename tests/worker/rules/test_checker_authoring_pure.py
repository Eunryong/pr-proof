"""app/worker/checker_authoring_pure.py: 검사 방법 계획, 캐시 키, 프롬프트, 예시 확인."""

from dataclasses import replace
from typing import Final

import pytest

from app.worker.core.checker_pure import CheckerKind
from app.worker.core.rule_pure import CheckMethod, Example, Rule, RuleSource
from app.worker.rules.checker_authoring_pure import (
    ANSWER_SCHEMA,
    EXAMPLE_FAILURES_TO_JUDGE,
    MAX_MODEL_EXAMPLES,
    PATH_PROMPT_VERSION,
    SYSTEM_PROMPT,
    Answer,
    AnswerExample,
    Coverage,
    Draft,
    Shortfall,
    Unchecked,
    answer_schema,
    build_prompt,
    checker_key,
    judge_examples,
    judge_route,
    judged_after_failures,
    kind_for_attempt,
    model_examples,
    parse_answer,
    plan_check,
    route_before_run,
    shortfall,
    system_prompt,
)

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


def test_format_switches_early_once_the_model_says_ast_grep_cannot_express_the_rule() -> None:
    switched = [kind_for_attempt(n, RULE, ast_grep_unfit=True) for n in (2, 3)]
    assert switched == ["python-ast", "python-ast"]
    path_rule = replace(RULE, check=CheckMethod("path"), examples=(), scope=())
    assert kind_for_attempt(2, path_rule, ast_grep_unfit=True) == "path-pattern"


def test_unfit_is_asked_only_of_ast_grep_and_leaves_the_path_prompt_version_alone() -> None:
    assert "unfit" in answer_schema("ast-grep")["required"]
    assert "unfit" not in answer_schema("python-ast")["required"]
    assert answer_schema("path-pattern") == ANSWER_SCHEMA


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


def test_model_examples_beyond_the_shortfall_are_all_checked() -> None:
    """규칙이 덮는 코드 모양마다 위반 예시를 하나씩 더 쓸 수 있다. 더 쓴 예시도 예시 확인을 거치도록
    모두 남는다. 상한(MAX_MODEL_EXAMPLES)을 넘는 것만 버린다."""
    shapes = [(f"x{n} = []\n\n\ndef f():\n    x{n}.append({n})\n", True, [5]) for n in range(6)]
    passing = [("def g():\n    y = []\n    y.append(1)\n", False, []), ("z = 1\n", False, [])]
    kept, problems = model_examples((), _answer(*shapes, *passing))
    assert problems == [] and len(kept) == 8 == MAX_MODEL_EXAMPLES
    assert sum(1 for e in kept if e.expect_lines) == 6


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
    from app.worker.core.cache_key_pure import prompt_version
    from app.worker.rules import checker_authoring_pure

    changed = prompt_version("다른 지시", checker_authoring_pure.ANSWER_SCHEMA, "")
    assert len(checker_authoring_pure.PROMPT_VERSION) == 12
    assert checker_authoring_pure.PROMPT_VERSION != changed


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


# --- 모델 판단으로 보내는 때 (4.4 "의미 규칙을 모델 판단으로 보내는 때") ---


def _draft(coverage: Coverage = "all", unfit: str = "", kind: CheckerKind = "ast-grep") -> Draft:
    uncovered = "" if coverage == "all" else "뜻을 읽어야 한다"
    return Draft(kind=kind, body="x\n", examples=(), example_problems=(), model="m",
                 unfit=unfit, coverage=coverage, uncovered=uncovered)  # fmt: skip


@pytest.mark.parametrize(
    ("coverage", "unfit", "route"),
    [
        ("all", "", None),
        ("all", "스코프를 따져야 한다", None),  # unfit은 형식의 한계다: Python AST로 바꾼다
        ("part", "", "part"),
        ("part", "스코프를 따져야 한다", "part"),  # 검사기의 몫이 형식보다 먼저다
        ("none", "", "none"),
    ],
)
def test_part_and_none_answers_go_to_judgment_before_any_run(
    coverage: Coverage, unfit: str, route: str | None
) -> None:
    assert route_before_run(_draft(coverage, unfit)) == route
    assert route_before_run(_draft(coverage, kind="python-ast")) == route


@pytest.mark.parametrize(("failures", "judged"), [(0, False), (1, False), (2, True), (3, True)])
def test_second_failed_example_check_sends_a_content_rule_to_judgment(
    failures: int, judged: bool
) -> None:
    assert EXAMPLE_FAILURES_TO_JUDGE == 2
    assert judged_after_failures(RULE, failures) is judged
    assert judged_after_failures(PATH_RULE, failures) is False  # 경로 규칙은 보내지 않는다


def test_saved_route_reads_known_values_and_unknown_ones_as_attempts() -> None:
    known = ("none", "part", "examples", "attempts")
    assert [judge_route(v) for v in known] == list(known)
    assert judge_route(None) is None
    assert judge_route("later") == "attempts"


def test_coverage_is_asked_of_content_checkers_only() -> None:
    for kind in ("ast-grep", "python-ast"):
        schema = answer_schema(kind)
        assert {"coverage", "uncovered"} <= set(schema["required"])
        assert schema["properties"]["coverage"]["enum"] == ["all", "part", "none"]
        assert "coverage" in SYSTEM_PROMPT[kind]
    assert "coverage" not in answer_schema("path-pattern")["properties"]
    assert "coverage" not in system_prompt("path-pattern")


def test_answer_without_coverage_reads_as_all() -> None:
    assert parse_answer({"checker": "x", "examples": []}).coverage == "all"
    parsed = parse_answer({"checker": "", "examples": [], "coverage": "none", "uncovered": "뜻"})
    assert (parsed.coverage, parsed.uncovered) == ("none", "뜻")
    with pytest.raises(ValueError):
        parse_answer({"checker": "", "examples": [], "coverage": "most"})


def test_path_prompt_version_is_pinned_so_a_change_to_it_is_deliberate() -> None:
    # 이 값이 바뀌면 경로 검사기를 모두 다시 쓴다. 내용 규칙의 지시문만 고칠 때는 그대로여야 한다
    assert PATH_PROMPT_VERSION == "6117d2bfd661"


def test_format_and_records_go_into_their_places_in_the_body() -> None:
    for kind in ("ast-grep", "python-ast"):
        plain, with_records = system_prompt(kind), system_prompt(kind, records=True)
        assert "{{" not in plain and "{{" not in with_records
        assert "<<<RECORDS" not in plain and "<<<RECORDS" in with_records
        # 형식 조각은 "검사기를 쓴다" 절 안에, 제약보다 앞에 들어간다
        assert (
            plain.index("### 4. 검사기를 쓴다")
            < plain.index("검사기의 형식은")
            < plain.index("## 제약")
        )
    assert "ast-grep 규칙(YAML 문서 하나)" in system_prompt("ast-grep")
    assert "Python 스크립트 하나" in system_prompt("python-ast")
    assert "$$$ARGS" in system_prompt("ast-grep")  # 조각의 `$`는 그대로다


def test_the_path_prompt_stands_alone_and_takes_the_records_note_too() -> None:
    plain, with_records = system_prompt("path-pattern"), system_prompt("path-pattern", records=True)
    assert "{{" not in plain and "<<<RECORDS" not in plain
    assert "<<<RECORDS" in with_records and with_records.index("<<<RECORDS") > with_records.index(
        "## 제약"
    )
