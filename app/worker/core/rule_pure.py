"""규칙과 규칙 묶음 (docs/DESIGN.md 7.1). 데이터와 id 계산만 한다.

규칙 읽기(4.3) 직후의 `check`와 `fix`는 모델이 준 힌트다. 검사 방법 정하기(4.4)가 확정한다.
"""

import hashlib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from typing import Final, Literal

from pydantic import TypeAdapter

# 인용 확인과 규칙 id에서 무시하는 마크다운 강조 기호. 모델이 `print`를 print로 옮겨도 같은 문장이다
_EMPHASIS: Final = re.compile(r"[`*]")
_SPACE: Final = re.compile(r"\s+")
_NOT_ALNUM: Final = re.compile(r"[^a-z0-9]+")
# 내용을 보는 검사기(ast-grep, Python AST)의 검사 대상 (7.7 Python 프로필)
PYTHON_SUFFIXES: Final = (".py", ".pyi")

# 규칙을 무엇을 보고 확인하는지 (4.3). code: 바뀐 코드 줄(실행 결과·생성물에 대한 규칙이면 그것을
# 만드는 코드). pr: PR 자체(커밋 메시지, 제목·본문, 브랜치 이름, 바뀐 파일의 종류). run:
# 파이프라인이 이미 돌리는 테스트·정적 검사의 결과
Target = Literal["code", "pr", "run"]
# run 규칙이 말하는 실행 결과: tests는 샌드박스 테스트, lint는 도구 설정의 규칙 묶음(ruff)이다
RunPart = Literal["tests", "lint"]


@dataclass(frozen=True)
class RuleSource:
    file: str  # "CONTRIBUTING.md", "pyproject.toml"
    section: str  # 문서의 제목 경로. 예: "코드 스타일 > 상수". 문서 맨 위 제목은 넣지 않는다
    blob_sha: str  # 읽은 파일의 git blob 해시


@dataclass(frozen=True)
class CheckMethod:
    # path는 규칙 읽기의 힌트다: 파일의 이름·경로만 보고 가리는 규칙이라 경로 패턴 검사기를 쓴다
    kind: Literal["tool", "checker", "path", "model", "none"]
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
    # 효과 확인(`checked_exclude`)이 이 PR의 base 파일 목록에 맞춰 보고 버린 추정. 코멘트에 보인다
    exclude_dropped: str = ""
    # 확인 대상. 모델이 고른다. code는 도구·검사기·모델 판단, pr은 PR 단위 확인, run은 테스트와
    # 정적 검사의 결과로 답한다
    target: Target = "code"
    runs: tuple[RunPart, ...] = ()  # target이 run일 때 규칙이 말하는 실행 결과


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
    pipeline_version: str  # 문서 고르기와 원문 확인의 버전 (app/worker/rules/rule_reading_pure.py)
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


def looks_at_paths(rule: Rule) -> bool:
    """파일의 내용이 아니라 이름·경로를 보는 규칙인지 (경로 패턴 검사기)."""
    return rule.check.kind == "path"


def checked_file(rule: Rule, path: str) -> bool:
    """규칙의 검사 대상 파일인지. 경로 규칙은 모든 파일, 그 밖은 Python 파일이다 (7.7)."""
    return applies_to(rule, path) and (looks_at_paths(rule) or path.endswith(PYTHON_SUFFIXES))


def starts_with_a_folder(pattern: str) -> bool:
    """첫 마디가 글자 그대로의 폴더 이름인지. `**`나 `*.py`처럼 저장소 전체를 뺄 수 있는 glob이
    아니다."""
    first, _, rest = pattern.partition("/")
    return bool(rest) and not (set("*?[") & set(first))


def checked_exclude(rule: Rule, base_files: Sequence[str]) -> Rule:
    """추정으로 뺀 경로의 효과 확인 (4.3). base 커밋의 파일 목록에 맞춰 보고 너무 많이 빼는 glob을
    버린다.

    규칙이 적용되는 파일(적용 범위 안의 검사 대상 파일)의 절반 이상을 빼는 glob은 버린다. 남은
    glob을 합쳐 절반 이상을 빼면 모두 버린다. 적용되는 파일이 base에 없어 잴 수 없으면 첫 마디가
    폴더 이름인 glob만 받는다. 버리면 범위가 넓은 쪽으로 남고, 버린 사실은 `exclude_dropped`에
    남는다.
    """
    if not rule.exclude:
        return rule
    bare = replace(rule, exclude=(), exclude_reason="")
    applicable = [f for f in base_files if checked_file(bare, f)]
    total = len(applicable)
    kept, dropped = [], []
    for glob in rule.exclude:
        removed = sum(1 for f in applicable if in_scope((glob,), f))
        if total == 0:
            if starts_with_a_folder(glob):
                kept.append(glob)
            else:
                dropped.append(f"{glob}: 적용되는 파일이 base에 없어 효과를 잴 수 없음")
        elif removed * 2 >= total:
            dropped.append(f"{glob}: 적용되는 파일 {total}개 중 {removed}개를 뺌")
        else:
            kept.append(glob)
    if kept and total:
        removed = sum(1 for f in applicable if in_scope(tuple(kept), f))
        if removed * 2 >= total:
            dropped.append(f"{', '.join(kept)}: 합쳐서 적용되는 파일 {total}개 중 {removed}개를 뺌")
            kept = []
    if not dropped:
        return rule
    return replace(
        rule,
        exclude=tuple(kept),
        exclude_reason=rule.exclude_reason if kept else "",
        exclude_dropped="; ".join(dropped),
    )


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
