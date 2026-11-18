"""규칙과 규칙 묶음 (docs/DESIGN.md 7.1). 데이터와 id 계산만 한다.

규칙 읽기(4.3) 직후의 `check`와 `fix`는 모델이 준 힌트다. 검사 방법 정하기(4.4)가 확정한다.
"""

import hashlib
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Final, Literal

from pydantic import TypeAdapter

# 인용 확인과 규칙 id에서 무시하는 마크다운 강조 기호. 모델이 `print`를 print로 옮겨도 같은 문장이다
_EMPHASIS: Final = re.compile(r"[`*]")
_SPACE: Final = re.compile(r"\s+")
_NOT_ALNUM: Final = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class RuleSource:
    file: str  # "CONTRIBUTING.md", "pyproject.toml"
    section: str  # 문서의 제목 경로. 예: "코드 스타일 > 상수". 문서 맨 위 제목은 넣지 않는다
    blob_sha: str  # 읽은 파일의 git blob 해시


@dataclass(frozen=True)
class CheckMethod:
    kind: Literal["tool", "checker", "model", "none"]
    tool: str = ""  # "ruff", "ruff-format"
    params: Mapping[str, object] = field(default_factory=dict)  # 예: {"codes": ["N802"]}
    checker_id: str = ""
    reason: str = ""  # none·model로 정한 이유


@dataclass(frozen=True)
class Example:
    code: str
    expect_lines: tuple[int, ...]  # 위반 예시면 잡혀야 할 줄(1부터), 정상 예시면 빈 튜플
    written_by: Literal["document", "model"]


@dataclass(frozen=True)
class Rule:
    id: str  # 문서 규칙: R-<파일 약칭>-<원문 해시 8자>. 도구 규칙: 도구의 코드
    text: str  # 원문 인용 (출처 문서의 그 절에 있음을 확인한 것)
    source: RuleSource
    origin: Literal["document", "tool_config"]
    scope: tuple[str, ...]  # 적용 범위 glob. 비면 저장소 전체
    check: CheckMethod
    fix: Literal["mechanical", "model", "none"]
    examples: tuple[Example, ...] = ()
    # 문서가 범위를 밝히지 않은 규칙에서 모델이 뺀 경로 glob과 그 이유 (4.3). 좁히기만 한다:
    # 빼는 목록이라 범위를 넓힐 수 없다. 문서가 범위를 밝힌 규칙에는 비어 있다
    exclude: tuple[str, ...] = ()
    exclude_reason: str = ""


@dataclass(frozen=True)
class UnreadDoc:
    path: str
    reason: str


@dataclass(frozen=True)
class DroppedRule:
    """모델이 답했지만 원문 확인에서 버린 규칙. 코멘트와 로그에 보인다."""

    file: str
    section: str
    quote: str
    reason: str


@dataclass(frozen=True)
class RuleSet:
    rules: tuple[Rule, ...]
    sources: tuple[tuple[str, str], ...]  # 읽은 문서의 (경로, blob 해시)
    pipeline_version: str  # 문서 고르기와 원문 확인의 버전 (models/rule_reading.py)
    prompt_version: str
    model: str  # 답한 모델의 전체 id
    unread: tuple[UnreadDoc, ...] = ()
    dropped: tuple[DroppedRule, ...] = ()

    def to_json(self) -> str:
        return _RULESET.dump_json(self).decode()

    @classmethod
    def from_json(cls, text: str) -> "RuleSet":
        return _RULESET.validate_json(text)


_RULESET: Final = TypeAdapter(RuleSet)


def normalize(text: str) -> str:
    """인용 확인과 규칙 id에 쓰는 형태. 공백을 하나로 모으고 강조 기호(`, *)를 뺀다."""
    text = unicodedata.normalize("NFC", text)
    return _SPACE.sub(" ", _EMPHASIS.sub("", text)).strip()


def rule_id(doc_path: str, quote: str) -> str:
    """`R-<파일 약칭>-<정리한 원문의 해시 앞 8자>`. 문서의 다른 부분이 바뀌어도 그대로다."""
    stem = _NOT_ALNUM.sub("-", PurePosixPath(doc_path).stem.lower()).strip("-") or "doc"
    digest = hashlib.sha256(normalize(quote).encode()).hexdigest()[:8]
    return f"R-{stem}-{digest}"


def applies_to(rule: Rule, path: str) -> bool:
    """규칙이 path(저장소 기준)에 적용되는지: 적용 범위 안이고 추정으로 뺀 경로가 아니다."""
    return in_scope(rule.scope, path) and not (rule.exclude and in_scope(rule.exclude, path))


def in_scope(scope: tuple[str, ...], path: str) -> bool:
    """path(저장소 기준)가 규칙의 적용 범위 glob 중 하나에 드는지. 범위가 비면 저장소 전체다.

    `**`는 폴더 여러 단계, `*`·`?`는 한 단계 안에서만 맞춘다 (`orders/*.py`는 `orders/a/b.py`를
    담지 않는다). `orders/**`는 `orders` 아래 모든 파일이다.
    """
    return not scope or any(_glob_regex(pattern).fullmatch(path) for pattern in scope)


def _glob_regex(pattern: str) -> re.Pattern[str]:
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "[" and (end := pattern.find("]", i + 1)) > i + 1:
            body = pattern[i + 1 : end]
            negate = body.startswith("!")
            body = (body[1:] if negate else body).replace("^", r"\^")
            out.append(f"[{'^' if negate else ''}{body}]")
            i = end + 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))
