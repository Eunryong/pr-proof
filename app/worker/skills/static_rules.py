"""static 스킬의 AST 규칙: 반복 리터럴과 함수가 바꾸는 모듈 수준 가변 상태 (docs/DESIGN.md F6).

PR 파일을 호스트에서 `ast.parse`로 읽기만 한다 (코드를 실행하지 않는다).
판정은 결정적이고, 대상 리포가 상수로 두고 바꾸지 않는 값은 위반으로 보지 않는다.
"""

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final

REPEAT_CODE: Final = "STATIC-REPEAT"
MUTABLE_CODE: Final = "STATIC-MUTABLE"
DEFAULT_MIN_REPEATS: Final = 3
MAX_LITERAL_CHARS: Final = 40  # 메시지에 보이는 리터럴 길이

# 반복돼도 상수로 뺄 이유가 없는 값 (ruff PLR2004가 허용하는 값과 같다)
_ALLOWED_NUMBERS: Final = frozenset({0, 1, -1})
_ALLOWED_STRINGS: Final = frozenset({"__main__"})
MIN_STRING_CHARS: Final = 2  # 이보다 짧은 문자열("", ",", "\n" 등)은 구분자로 보고 세지 않는다

TEST_DIRS: Final = frozenset({"test", "tests"})
_ALL: Final = "__all__"

_MUTABLE_FACTORIES: Final = frozenset(
    {"list", "dict", "set", "bytearray", "defaultdict", "OrderedDict", "Counter", "deque",
     "ChainMap"}
)  # fmt: skip
_MUTATING_METHODS: Final = frozenset(
    {
        "append", "extend", "insert", "pop", "remove", "clear", "update", "setdefault",
        "popitem", "sort", "reverse", "add", "discard", "difference_update",
        "intersection_update", "symmetric_difference_update", "appendleft", "extendleft",
        "popleft", "rotate", "__setitem__", "__delitem__",
    }
)  # fmt: skip


@dataclass(frozen=True)
class StaticViolation:
    line: int
    code: str
    message: str


def is_test_file(path: str) -> bool:
    """테스트 파일인지. static 위반은 테스트 파일에서 찾지 않는다.

    테스트의 리터럴은 기대값이고, AI 수정은 테스트 파일을 바꿀 수 없다 (DESIGN.md F7).
    """
    p = PurePosixPath(path)
    name = p.name
    if name == "conftest.py" or name.startswith("test_") or name.endswith("_test.py"):
        return True
    return any(part in TEST_DIRS for part in p.parts[:-1])


def parse(source: str) -> ast.Module | None:
    """파싱하지 못하면 None. 문법 오류는 lint 스킬이 알린다."""
    try:
        return ast.parse(source)
    except (SyntaxError, ValueError):  # ValueError: 소스에 NUL 문자가 있음
        return None


# --- 반복 리터럴 ----------------------------------------------------------------------------

LiteralKey = tuple[str, object]  # (타입 이름, 값). 1·1.0·True가 같은 키가 되지 않게 타입을 붙인다


def _literal_value(node: ast.expr) -> object | None:
    """세는 리터럴의 값. 세지 않는 노드면 None. `-5`처럼 부호 붙은 숫자는 하나의 리터럴로 본다."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub | ast.UAdd):
        inner = node.operand
        if isinstance(inner, ast.Constant) and _is_number(inner.value):
            return -inner.value if isinstance(node.op, ast.USub) else inner.value
        return None
    if not isinstance(node, ast.Constant):
        return None
    value = node.value
    if _is_number(value) or isinstance(value, str | bytes):
        return value
    return None  # None, True/False, Ellipsis


def _is_number(value: object) -> bool:
    return isinstance(value, int | float | complex) and not isinstance(value, bool)


def _counted(value: object) -> bool:
    if isinstance(value, str | bytes):
        return len(value) >= MIN_STRING_CHARS and value not in _ALLOWED_STRINGS
    return value not in _ALLOWED_NUMBERS


def _is_definition(target: ast.expr) -> bool:
    """값을 세지 않는 대입 대상: 대문자 이름 상수의 정의 자체와 `__all__`."""
    return isinstance(target, ast.Name) and (target.id.isupper() or target.id == _ALL)


def _is_docstring_like(stmt: ast.stmt) -> bool:
    """독스트링을 포함해, 식으로만 쓰인 문자열 문장."""
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


def _skipped_subtrees(node: ast.AST) -> Iterator[ast.AST]:
    """node의 자식 중 리터럴을 세지 않을 부분."""
    if isinstance(node, ast.Expr) and _is_docstring_like(node):
        yield node.value
    elif isinstance(node, ast.arg) and node.annotation is not None:
        yield node.annotation
    elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.returns is not None:
        yield node.returns
    elif isinstance(node, ast.AnnAssign):
        yield node.annotation
        if node.value is not None and _is_definition(node.target):
            yield node.value
    elif isinstance(node, ast.Assign):
        if all(_is_definition(t) for t in node.targets):
            yield node.value
    elif isinstance(node, ast.Dict):
        yield from (k for k in node.keys if k is not None)
    elif isinstance(node, ast.Subscript):
        yield node.slice  # 인덱스·키(`data["id"]`, `row[2]`)와 `Literal["a"]`
    elif isinstance(node, ast.JoinedStr):
        yield from node.values


def _in_counted_context(node: ast.expr, parent: ast.AST | None) -> bool:
    """리터럴이 세는 자리에 있는지: 비교, 함수 호출의 인자, 연산식의 피연산자.

    list·tuple·set·dict 리터럴의 원소는 데이터로 보고 세지 않는다 (부모가 그 리터럴이므로
    여기서 걸러진다). 대입·return·기본값 자리의 값도 세지 않는다.
    """
    if isinstance(parent, ast.Compare | ast.BinOp | ast.BoolOp | ast.AugAssign | ast.keyword):
        return True
    return isinstance(parent, ast.Call) and node is not parent.func


def literal_occurrences(tree: ast.Module) -> dict[LiteralKey, list[int]]:
    """세는 리터럴별로 나온 줄 번호 (나온 순서, 같은 줄에 두 번이면 두 번).

    세는 자리는 비교, 함수 호출의 인자(키워드 인자 포함), 연산식(산술·논리·증분 대입)의
    피연산자뿐이다 (`_in_counted_context`). 그 안에서도 세지 않는 것: 독스트링·식 문장의
    문자열, 타입 주석·`Literal[...]`, 대문자 이름 상수의 정의 값, `__all__`, 첨자(인덱스·키),
    f-string 안, None·True·False, 0·1·-1, 2자 미만 문자열, `"__main__"`.
    """
    found: dict[LiteralKey, list[int]] = {}
    skipped: set[int] = set()
    stack: list[tuple[ast.AST, ast.AST | None]] = [(tree, None)]
    while stack:
        node, parent = stack.pop()
        if id(node) in skipped:
            continue
        skipped.update(id(n) for n in _skipped_subtrees(node))
        if isinstance(node, ast.expr):
            value = _literal_value(node)
            if value is not None:
                if _counted(value) and _in_counted_context(node, parent):
                    found.setdefault((type(value).__name__, value), []).append(node.lineno)
                continue  # `-5`의 안쪽 5를 다시 세지 않는다
        stack.extend((child, node) for child in ast.iter_child_nodes(node))
    for lines in found.values():
        lines.sort()
    return found


def _show(value: object) -> str:
    text = repr(value)
    if len(text) > MAX_LITERAL_CHARS:
        text = text[: MAX_LITERAL_CHARS - 1] + "…"
    return text


def repeated_literals(
    tree: ast.Module, changed: frozenset[int], min_repeats: int
) -> list[StaticViolation]:
    """파일에서 min_repeats번 이상 나온 리터럴 중, 한 번이라도 PR이 바꾼 줄에 나온 것.

    리터럴마다 하나씩, PR이 바꾼 첫 줄에 단다. 메시지에는 줄 번호·횟수를 넣지 않는다
    (수정 전후 비교에서 줄이 밀려도 같은 위반으로 보게).
    """
    violations = []
    for (_, value), lines in literal_occurrences(tree).items():
        if len(lines) < min_repeats:
            continue
        on_changed = [n for n in lines if n in changed]
        if not on_changed:
            continue
        violations.append(
            StaticViolation(
                line=on_changed[0],
                code=REPEAT_CODE,
                message=(
                    f"같은 리터럴 {_show(value)}이(가) 파일 안에서 {min_repeats}번 이상 쓰임."
                    " 이름 있는 상수로 추출"
                ),
            )
        )
    return sorted(violations, key=lambda v: (v.line, v.message))


# --- 모듈 수준 가변 상태 -----------------------------------------------------------------------


def _is_mutable_value(value: ast.expr | None) -> bool:
    if isinstance(value, ast.List | ast.Dict | ast.Set | ast.ListComp | ast.DictComp | ast.SetComp):
        return True
    if isinstance(value, ast.Call):
        func = value.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        return name in _MUTABLE_FACTORIES
    return False


def _module_statements(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    """모듈 최상위 문장. if·try·with 블록 안도 포함하고 def·class 안은 뺀다."""
    for stmt in body:
        yield stmt
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        for field in ("body", "orelse", "finalbody"):
            yield from _module_statements(getattr(stmt, field, []) or [])
        for handler in getattr(stmt, "handlers", []) or []:
            yield from _module_statements(handler.body)


def mutable_module_names(tree: ast.Module) -> dict[str, int]:
    """모듈 수준에서 가변 값(list·dict·set 리터럴·컴프리헨션, list()·defaultdict() 등)을
    받은 이름과 첫 대입 줄."""
    names: dict[str, int] = {}
    for stmt in _module_statements(tree.body):
        if isinstance(stmt, ast.Assign) and _is_mutable_value(stmt.value):
            targets = [t for t in stmt.targets if isinstance(t, ast.Name)]
        elif isinstance(stmt, ast.AnnAssign) and _is_mutable_value(stmt.value):
            targets = [stmt.target] if isinstance(stmt.target, ast.Name) else []
        else:
            continue
        for target in targets:
            names.setdefault(target.id, stmt.lineno)
    return names


_Function = ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda


def _own_nodes(func: _Function) -> Iterator[ast.AST]:
    """함수 자신의 본문 노드. 안쪽 함수·클래스의 본문은 뺀다 (기본값·데코레이터는 바깥 것)."""
    stack: list[ast.AST] = list(func.body) if isinstance(func.body, list) else [func.body]
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            stack.extend([*node.decorator_list, *node.args.defaults, *node.args.kw_defaults])
            continue
        if isinstance(node, ast.Lambda):
            stack.extend([*node.args.defaults, *node.args.kw_defaults])
            continue
        if isinstance(node, ast.ClassDef):
            stack.extend([*node.decorator_list, *node.bases, *node.keywords])
            stack.extend(node.body)  # 클래스 본문은 같은 함수 실행 중에 돈다
            continue
        stack.extend(n for n in ast.iter_child_nodes(node) if n is not None)


def _local_names(func: _Function) -> tuple[set[str], set[str]]:
    """(지역 이름, global 선언 이름). nonlocal 이름도 지역(바깥 함수의 것)으로 본다."""
    args = func.args
    local = {a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]}
    local |= {a.arg for a in (args.vararg, args.kwarg) if a is not None}
    declared_global: set[str] = set()
    for node in _own_nodes(func):
        if isinstance(node, ast.Global):
            declared_global.update(node.names)
        elif isinstance(node, ast.Nonlocal):
            local.update(node.names)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
            local.add(node.id)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            local.add(node.name)
        elif isinstance(node, ast.alias):
            local.add((node.asname or node.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            local.add(node.name)
    return local - declared_global, declared_global


def _mutated_name(node: ast.AST) -> str | None:
    """node가 이름의 내용을 바꾸면 그 이름. `X.append(...)`, `X[k] = v`, `X[k] += v`, `del X[k]`."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        owner = node.func.value
        if isinstance(owner, ast.Name) and node.func.attr in _MUTATING_METHODS:
            return owner.id
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store | ast.Del):
        if isinstance(node.value, ast.Name):
            return node.value.id
    return None


def _functions(tree: ast.Module) -> Iterator[tuple[_Function, list[_Function]]]:
    """(함수, 바깥 함수들). 클래스 안의 메서드도 포함한다."""

    def walk(node: ast.AST, outer: list[_Function]) -> Iterator[tuple[_Function, list[_Function]]]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _Function):
                yield child, outer
                yield from walk(child, [*outer, child])
            else:
                yield from walk(child, outer)

    return walk(tree, [])


def module_state_mutations(tree: ast.Module) -> dict[str, list[int]]:
    """함수 안에서 바뀌는 모듈 수준 가변 이름과 바꾸는 줄.

    바꾸는 것: 바꾸는 메서드 호출(append·update·setdefault 등), 첨자 대입·삭제,
    `global` 선언 뒤 다시 대입. 함수의 지역 이름(인자, 대입, import)이 가리면 세지 않는다.
    모듈 최상위에서 채우는 것(초기화)은 세지 않는다.
    """
    names = mutable_module_names(tree)
    found: dict[str, list[int]] = {}
    for func, outer in _functions(tree):
        local, declared_global = _local_names(func)
        shadowed = set(local)
        for enclosing in outer:  # 바깥 함수의 지역 이름이 가리는 경우
            outer_local, _ = _local_names(enclosing)
            shadowed |= outer_local - declared_global
        for node in _own_nodes(func):
            name = _mutated_name(node)
            if name is None and isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                name = node.id if node.id in declared_global else None
            if name in names and name not in shadowed:
                found.setdefault(name, []).append(getattr(node, "lineno", names[name]))
    return {name: sorted(set(lines)) for name, lines in found.items()}


def mutable_state(tree: ast.Module, changed: frozenset[int]) -> list[StaticViolation]:
    """함수가 바꾸는 모듈 수준 가변 상태. 정의 줄이나 바꾸는 줄이 PR이 바꾼 줄이면 이름마다 하나.

    상수로 두고 바꾸지 않는 dict·list(`Final[Mapping]` 등)는 위반이 아니다.
    """
    definitions = mutable_module_names(tree)
    violations = []
    for name, lines in module_state_mutations(tree).items():
        on_changed = sorted(n for n in {definitions[name], *lines} if n in changed)
        if not on_changed:
            continue
        violations.append(
            StaticViolation(
                line=on_changed[0],
                code=MUTABLE_CODE,
                message=(
                    f"모듈 수준 가변 상태 `{name}`을(를) 함수 안에서 바꿈."
                    " 인자·반환값이나 객체의 상태로 옮기기"
                ),
            )
        )
    return sorted(violations, key=lambda v: (v.line, v.message))
