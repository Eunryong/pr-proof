"""모델이 쓴 검사기 (docs/DESIGN.md 7.2). 데이터, id 계산, 실행 전 본문 확인.

검사기 본문은 모델이 쓴 믿지 않는 입력이다. 샌드박스에 넣기 전에 `validate_body`가 형식과 허용
범위를 본다. 통과한 본문만 실행하고, 거절 이유는 다음 회차의 모델에 넘긴다.
"""

import ast
import hashlib
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

import yaml

from app.models.rule import Example

CheckerKind = Literal["ast-grep", "python-ast"]

MAX_BODY_CHARS: Final = 20_000
# 이 단계의 검사기는 Python 파일만 본다 (7.7 Python 프로필)
AST_GREP_LANGUAGE: Final = "python"
# 그 밖의 키는 거절한다. fix·rewriters·transform은 코드를 바꾸는 키이고, files·ignores는 적용
# 범위를 규칙 밖에서 정한다. 적용 범위는 파이프라인이 규칙의 scope로 정한다
AST_GREP_KEYS: Final = frozenset(
    {"id", "language", "rule", "constraints", "utils", "message", "severity", "note"}
)


@dataclass(frozen=True)
class Checker:
    id: str  # 본문 해시 (checker_id)
    rule_id: str
    rule_text_hash: str
    kind: CheckerKind
    body: str  # 규칙 YAML 또는 스크립트
    examples: tuple[Example, ...]
    engine_version: str  # 검사 엔진 버전. 바뀌면 예시 확인을 다시 한다
    model: str = ""
    verified_at: datetime | None = None


def checker_id(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


def validate_body(kind: CheckerKind, body: str) -> str | None:
    """실행하면 안 되는 본문이면 그 이유, 괜찮으면 None."""
    if not body.strip():
        return "검사기 본문이 비어 있음"
    if len(body) > MAX_BODY_CHARS:
        return f"검사기 본문이 {MAX_BODY_CHARS}자를 넘음"
    if kind == "ast-grep":
        return _validate_ast_grep(body)
    return _validate_python_ast(body)


def _validate_ast_grep(body: str) -> str | None:
    try:
        if any(isinstance(event, yaml.AliasEvent) for event in yaml.parse(body)):
            return "YAML 별칭(*name)은 쓰지 않는다"
        documents = [d for d in yaml.safe_load_all(body) if d is not None]
    except yaml.YAMLError as e:
        return f"YAML을 읽지 못함: {str(e).splitlines()[0]}"
    if len(documents) != 1:
        return f"YAML 문서는 하나여야 함 ({len(documents)}개)"
    config = documents[0]
    if not isinstance(config, Mapping):
        return "규칙 YAML의 최상위가 매핑이 아님"
    extra = sorted(str(k) for k in config if k not in AST_GREP_KEYS)
    if extra:
        allowed = ", ".join(sorted(AST_GREP_KEYS))
        return f"쓸 수 없는 키: {', '.join(extra)} (쓸 수 있는 키: {allowed})"
    if config.get("language") != AST_GREP_LANGUAGE:
        return f"language는 {AST_GREP_LANGUAGE}여야 함 (받은 값: {config.get('language')!r})"
    if not isinstance(config.get("rule"), Mapping):
        return "rule이 없거나 매핑이 아님"
    utils = config.get("utils") or {}
    if not isinstance(utils, Mapping):
        return "utils가 매핑이 아님"
    unknown = sorted({str(m) for m in _matches(config) if m not in utils})
    if unknown:
        return f"matches가 이 파일의 utils에 없는 규칙을 가리킴: {', '.join(unknown)}"
    return None


def _matches(node: object) -> Iterator[object]:
    """규칙 트리 안의 모든 `matches:` 값."""
    if isinstance(node, Mapping):
        for key, value in node.items():
            if key == "matches":
                yield value
            yield from _matches(value)
    elif isinstance(node, list):
        for item in node:
            yield from _matches(item)


def _validate_python_ast(body: str) -> str | None:
    try:
        tree = ast.parse(body)
    except SyntaxError as e:
        return f"Python 문법 오류: {e.msg} (줄 {e.lineno})"
    outside = sorted(
        {name for name in _imported(tree) if name.split(".")[0] not in sys.stdlib_module_names}
    )
    if outside:
        return f"표준 라이브러리 밖의 모듈을 import함: {', '.join(outside)}"
    return None


def _imported(tree: ast.AST) -> Iterator[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            yield "." if node.level else node.module or ""
