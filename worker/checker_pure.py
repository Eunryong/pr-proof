"""모델이 쓴 검사기 (docs/DESIGN.md 7.2). 데이터, id 계산, 실행 전 본문 확인.

검사기 본문은 모델이 쓴 믿지 않는 입력이다. 샌드박스에 넣기 전에 `validate_body`가 형식과 허용
범위를 본다. 통과한 본문만 실행하고, 거절 이유는 다음 회차의 모델에 넘긴다.
"""

import ast
import hashlib
import json
import re
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Final, Literal

import yaml

from worker.rule_pure import Example
from worker.tools_pure import RUFF_CODE

# path-pattern은 코드가 아니라 선언(JSON)이다: 경로에 거는 정규식 조건. 파이프라인이 쓴 고정
# 스크립트가 샌드박스에서 맞춘다 (sandbox/checker_runner.py).
# ruff·ruff-format은 저장소의 도구다 (4.4 (a)). 본문은 선언(JSON)이다: ruff는 {"codes": [...]}(비면
# base 설정의 규칙 전부), ruff-format은 {}. 모델이 쓴 검사기가 아니다
CheckerKind = Literal["ast-grep", "python-ast", "path-pattern", "ruff", "ruff-format"]
TOOL_KINDS: Final = frozenset({"ruff", "ruff-format"})
PATH_PATTERN_KEYS: Final = frozenset({"applies_to", "condition", "pattern", "message"})
PATH_CONDITIONS: Final = ("must_match", "must_not_match")
MAX_REGEX_CHARS: Final = 300

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


def is_repo_path(path: str) -> bool:
    """검사기에 넘길 수 있는 저장소 기준 상대 경로인지. 모델이 쓴 경로 예시도 이것으로 본다."""
    p = PurePosixPath(path)
    return bool(path) and not p.is_absolute() and ".." not in p.parts and "\0" not in path


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
    if kind == "path-pattern":
        return _validate_path_pattern(body)
    if kind in TOOL_KINDS:
        return _validate_tool(kind, body)
    return _validate_python_ast(body)


def tool_codes(body: str) -> tuple[str, ...]:
    """ruff 검사기 본문의 코드. 비면 base 설정의 규칙 전부다. 본문은 `validate_body`를 거쳤다."""
    return tuple(json.loads(body).get("codes", ()))


def _validate_tool(kind: CheckerKind, body: str) -> str | None:
    try:
        decl = json.loads(body)
    except ValueError as e:
        return f"JSON을 읽지 못함: {e}"
    if not isinstance(decl, dict):
        return "선언의 최상위가 객체가 아님"
    allowed = {"codes"} if kind == "ruff" else set()
    extra = sorted(str(k) for k in decl if k not in allowed)
    if extra:
        return f"쓸 수 없는 키: {', '.join(extra)}"
    codes = decl.get("codes", [])
    if not isinstance(codes, list) or not all(
        isinstance(c, str) and RUFF_CODE.match(c) for c in codes
    ):
        return "codes는 ruff 규칙 코드의 목록이어야 함"
    return None


def _validate_path_pattern(body: str) -> str | None:
    """선언의 모양과 정규식 문법만 본다. 맞추는 일은 시간 제한이 있는 샌드박스가 한다."""
    try:
        decl = json.loads(body)
    except ValueError as e:
        return f"JSON을 읽지 못함: {e}"
    if not isinstance(decl, dict):
        return "선언의 최상위가 객체가 아님"
    extra = sorted(str(k) for k in decl if k not in PATH_PATTERN_KEYS)
    if extra:
        allowed = ", ".join(sorted(PATH_PATTERN_KEYS))
        return f"쓸 수 없는 키: {', '.join(extra)} (쓸 수 있는 키: {allowed})"
    if decl.get("condition") not in PATH_CONDITIONS:
        allowed = " 또는 ".join(PATH_CONDITIONS)
        return f"condition은 {allowed}여야 함 (받은 값: {decl.get('condition')!r})"
    if not isinstance(decl.get("pattern"), str) or not decl["pattern"]:
        return "pattern이 없거나 문자열이 아님"
    for key in ("applies_to", "pattern"):
        value = decl.get(key, "")
        if not isinstance(value, str):
            return f"{key}가 문자열이 아님"
        if len(value) > MAX_REGEX_CHARS:
            return f"{key}가 {MAX_REGEX_CHARS}자를 넘음"
        try:
            re.compile(value)
        except re.error as e:
            return f"{key}의 정규식이 틀림: {e}"
    if not isinstance(decl.get("message", ""), str):
        return "message가 문자열이 아님"
    return None


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
