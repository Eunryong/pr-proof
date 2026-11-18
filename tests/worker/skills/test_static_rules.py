"""worker/skills/static_rules.py: 반복 리터럴·모듈 수준 가변 상태 AST 규칙."""

import ast
from typing import Final

import pytest

from app.worker.skills import static_rules as rules


def _tree(source: str) -> ast.Module:
    tree = rules.parse(source)
    assert tree is not None
    return tree


def _counts(source: str) -> dict[object, int]:
    return {
        value: len(lines) for (_, value), lines in rules.literal_occurrences(_tree(source)).items()
    }


# --- 테스트 파일·파싱 ---


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/test_a.py", True),
        ("pkg/tests/helpers.py", True),
        ("test/x.py", True),
        ("conftest.py", True),
        ("pkg/a_test.py", True),
        ("test_a.py", True),
        ("pkg/testing.py", False),
        ("pkg/contest.py", False),
        ("latest/a.py", False),
        ("pkg/attest_x.py", False),
    ],
)
def test_is_test_file(path: str, expected: bool) -> None:
    assert rules.is_test_file(path) is expected


def test_parse_failure_is_none() -> None:
    assert rules.parse("def f(:\n") is None
    assert rules.parse("x = 1\0\n") is None


# --- 반복 리터럴: 세는 것과 세지 않는 것 ---


def test_counts_strings_numbers_bytes_and_signed_numbers() -> None:
    counts = _counts('f("ab", 42, b"xy", -5, 5, 2.5, 3j)\n')
    assert counts == {"ab": 1, 42: 1, b"xy": 1, -5: 1, 5: 1, 2.5: 1, 3j: 1}


def test_type_distinguishes_equal_values() -> None:
    found = rules.literal_occurrences(_tree("f(2, 2.0)\n"))
    assert set(found) == {("int", 2), ("float", 2.0)}


@pytest.mark.parametrize(
    "source",
    [
        'def f():\n    """docstring ab"""\n',
        '"""module doc"""\n',
        '"bare expression string"\n',
        "f(None, True, False, ...)\n",
        "f(0, 1, -1, 0.0, 1.0, +1)\n",
        'f("", "a", ",", "\\n")\n',  # 2자 미만
        'if __name__ == "__main__":\n    pass\n',
        'def f(x: "ab") -> "cd":\n    y: "ef" = g()\n',  # 타입 주석
        'X = Literal["ab", "cd"]\n',
        'NAME = "ab"\nOTHER: Final = 42\n',  # 상수 정의 자체
        '__all__ = ["ab", "cd"]\n',
        '__all__: list[str] = ["ab"]\n',
        'd = {"key": v}\n',  # dict 키
        'x = data["key"]\ny = row[2]\nz = s[2:5]\n',  # 첨자
        'x = f"ab{v}cd{w:>10}"\n',  # f-string
    ],
)
def test_not_counted(source: str) -> None:
    assert _counts(source) == {}


def test_dict_values_and_lowercase_assignments_are_counted() -> None:
    assert _counts('d = {"key": "val"}\nname = "ab"\n') == {"val": 1, "ab": 1}


# --- 반복 리터럴: 위반 ---

REPEATED: Final = 'def f():\n    a("ab")\n    b("ab")\n    c("ab")\n'


def test_repeated_reported_once_at_first_changed_line() -> None:
    (v,) = rules.repeated_literals(_tree(REPEATED), frozenset({3, 4}), 3)
    assert (v.line, v.code) == (3, rules.REPEAT_CODE)
    assert "'ab'" in v.message
    assert "줄" not in v.message  # 줄 번호가 밀려도 같은 메시지


def test_repeated_needs_a_changed_occurrence() -> None:
    assert rules.repeated_literals(_tree(REPEATED), frozenset({1}), 3) == []
    assert rules.repeated_literals(_tree(REPEATED), frozenset(), 3) == []


@pytest.mark.parametrize(("min_repeats", "reported"), [(2, 1), (3, 1), (4, 0)])
def test_min_repeats_boundary(min_repeats: int, reported: int) -> None:
    found = rules.repeated_literals(_tree(REPEATED), frozenset({2}), min_repeats)
    assert len(found) == reported


def test_same_line_occurrences_count() -> None:
    found = rules.repeated_literals(_tree('f("ab", "ab", "ab")\n'), frozenset({1}), 3)
    assert len(found) == 1


def test_long_literal_is_truncated_in_message() -> None:
    long = "x" * 100
    (v,) = rules.repeated_literals(_tree(f'f("{long}")\n' * 3), frozenset({1}), 3)
    assert long not in v.message and "…" in v.message


# --- 모듈 수준 가변 상태 ---


def test_mutable_module_names() -> None:
    source = (
        "A = []\nB: dict[str, int] = {}\nC = set()\nD = collections.defaultdict(list)\n"
        "E = [x for x in y]\nF = (1, 2)\nG = frozenset()\nH = Config()\nI, J = [], []\n"
        "if cond:\n    K = {}\ntry:\n    L = []\nexcept E:\n    M = []\n"
        "def f():\n    N = []\nclass C:\n    O = []\n"
    )
    assert rules.mutable_module_names(_tree(source)) == {
        "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "K": 11, "L": 13, "M": 15,
    }  # fmt: skip


@pytest.mark.parametrize(
    "body",
    [
        "STATE.append(1)",
        "STATE.update(x=1)",
        "STATE.setdefault('k', 1)",
        "STATE['k'] = 1",
        "STATE['k'] += 1",
        "del STATE['k']",
        "STATE[1:2] = []",
        "global STATE\n    STATE = []",
        "global STATE\n    STATE += [1]",
    ],
)
def test_mutation_in_function_detected(body: str) -> None:
    source = f"STATE = []\n\n\ndef f():\n    {body}\n"
    assert set(rules.module_state_mutations(_tree(source))) == {"STATE"}


@pytest.mark.parametrize(
    "source",
    [
        "STATE = []\nSTATE.append(1)\nSTATE['k'] = 2\n",  # 모듈 수준 초기화
        "STATE = {}\n\ndef f():\n    return STATE.get('k')\n",  # 읽기만
        "STATE = {}\n\ndef f():\n    return STATE['k']\n",
        "STATE = []\n\ndef f(STATE):\n    STATE.append(1)\n",  # 인자가 가림
        "STATE = []\n\ndef f():\n    STATE = []\n    STATE.append(1)\n",  # 지역 이름
        "STATE = []\n\ndef f():\n    for STATE in x:\n        STATE.append(1)\n",
        "STATE = []\n\ndef f():\n    import STATE\n    STATE.append(1)\n",
        "STATE = []\n\ndef f():\n    STATE = []\n\n    def g():\n        STATE.append(1)\n",
        "STATE = []\n\ndef f():\n    STATE = []\n\n    def g():\n        nonlocal STATE\n"
        "        STATE = [1]\n",
        "STATE = (1, 2)\n\ndef f():\n    global STATE\n    STATE = (3,)\n",  # 가변 값이 아님
        "STATE = []\n\ndef f():\n    other.append(1)\n    STATE.copy()\n",
    ],
)
def test_not_a_mutation(source: str) -> None:
    assert rules.module_state_mutations(_tree(source)) == {}


def test_nested_function_and_method_and_lambda_mutations() -> None:
    source = (
        "STATE = []\n"
        "def f():\n    def g():\n        STATE.append(1)\n"
        "class C:\n    def m(self):\n        STATE.clear()\n"
        "h = lambda: STATE.pop()\n"
    )
    assert rules.module_state_mutations(_tree(source)) == {"STATE": [4, 7, 8]}


def test_global_in_inner_function_beats_outer_local() -> None:
    source = (
        "STATE = []\n"
        "def f():\n    STATE = 1\n    def g():\n        global STATE\n        STATE = []\n"
    )
    assert rules.module_state_mutations(_tree(source)) == {"STATE": [6]}


MUTATED: Final = "CACHE = {}\n\n\ndef put(k, v):\n    CACHE[k] = v\n"


@pytest.mark.parametrize(("changed", "line"), [({1}, 1), ({5}, 5), ({1, 5}, 1)])
def test_mutable_state_reported_once_on_changed_line(changed: set[int], line: int) -> None:
    (v,) = rules.mutable_state(_tree(MUTATED), frozenset(changed))
    assert (v.line, v.code) == (line, rules.MUTABLE_CODE)
    assert "`CACHE`" in v.message


def test_mutable_state_not_on_changed_lines() -> None:
    assert rules.mutable_state(_tree(MUTATED), frozenset({2, 3, 4})) == []


def test_unmutated_module_dict_is_not_a_violation() -> None:
    source = "LIMITS: Final[Mapping[str, int]] = {'a': 1}\n\ndef f():\n    return LIMITS['a']\n"
    assert rules.mutable_state(_tree(source), frozenset({1, 2, 3, 4})) == []
