"""저장소의 도구 (docs/DESIGN.md 2.3, 4.3 도구 설정, 4.4 도구 목록, 7.7 Python 프로필의 도구).

데이터와 결정적 판단만 한다. 받기와 실행은 worker/sandbox/toolcache.py와 worker/lang/python.py가
한다.

- 도구 목록(`TOOL_LIST`)은 밖에서 받아 오는 도구뿐이다. 문서 규칙의 확인 방법을 고르는 모델은 이
  목록 안에서만 고른다. 플러그인 로더는 만들지 않는다.
- 도구 설정에서 온 규칙 묶음: base의 ruff 설정 파일이 있으면 "이 설정대로 ruff를 통과한다"는 규칙
  하나(`CONFIG_RULE_ID`)가 생긴다. 그 규칙의 위반은 ruff의 코드(`F401`)를 규칙 id로 갖는다 (4.3).
  설정 파일이 없으면 이 규칙도 없다. 파이프라인이 고른 ruff 규칙은 없다 (2.2).
- 버전: 저장소의 의존성 파일이 `ruff==X.Y.Z`로 고정했으면 그 버전, 아니면 파이프라인의 기본 버전.
"""

import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from worker.cache_key_pure import text_hash
from worker.rule_pure import CheckMethod, Rule, RuleSource

RUFF: Final = "ruff"
RUFF_FORMAT: Final = "ruff-format"
# 모델에 보이는 도구 목록 (4.4의 표). 바꾸면 TOOLS_VERSION이 바뀌어 도구 선택을 다시 묻는다
TOOL_LIST: Final[Mapping[str, str]] = {
    RUFF: (
        "ruff가 가진 린트 규칙. 규칙 코드를 하나 이상 고른다 (예: F401 안 쓰는 import, T201 print, "
        "PLR2004 비교의 매직 값, PLW0603 global 문). 기계적 수정은 ruff의 안전한 수정이 있는 "
        "코드만"
    ),
    RUFF_FORMAT: (
        "ruff format의 코드 모양 (들여쓰기, 줄 바꿈, 따옴표, 공백). 코드를 고르지 않는다. 기계적 "
        "수정은 base가 ruff format을 따르는 저장소에서만"
    ),
}
TOOLS_VERSION: Final = text_hash("\0".join(f"{k}={v}" for k, v in sorted(TOOL_LIST.items())))[:12]
# ruff가 설정 파일을 찾는 우선순위와 같다. pyproject.toml은 [tool.ruff]가 있을 때만 ruff 설정이다
RUFF_CONFIG_FILES: Final = (".ruff.toml", "ruff.toml", "pyproject.toml")
# 도구 설정에서 온 규칙 묶음의 id. `rules.disabled`에 적으면 묶음 전체가 꺼진다
CONFIG_RULE_ID: Final = "ruff"
RUFF_CODE: Final = re.compile(r"^[A-Z]{1,4}[0-9]{1,4}$")
MAX_CODES: Final = 10
REQUIREMENTS: Final = re.compile(r"^requirements[\w.-]*\.txt$")
# `ruff==0.17.4`, `ruff[extra] == 0.17.4 ; python_version > "3"`. 정확히 고정한 것만 본다
_PIN: Final = re.compile(
    r"^\s*ruff\s*(?:\[[^\]]*\])?\s*===?\s*([0-9]+(?:\.[0-9]+){1,3})\s*(?:[;#].*)?$"
)


@dataclass(frozen=True)
class RuffConfig:
    name: str  # 저장소 루트의 설정 파일 이름
    text: str
    blob_sha: str


@dataclass(frozen=True)
class RuffSetup:
    """이 저장소의 ruff. 받는 것은 규칙 묶음이 ruff를 쓸 때뿐이다 (2.3)."""

    version: str
    pinned_by: str = ""  # 버전을 고정한 파일. 비면 파이프라인의 기본 버전
    config: RuffConfig | None = None  # base의 ruff 설정. 없으면 도구 설정의 규칙도 없다
    available: bool = True  # 파이프라인이 해시를 고정한 버전인지

    @property
    def label(self) -> str:
        who = f"{self.pinned_by}가 고정" if self.pinned_by else "기본 버전"
        return f"ruff {self.version} ({who})"


def ruff_config_name(files: Mapping[str, str]) -> tuple[str | None, str | None]:
    """(쓸 ruff 설정 파일 이름, 읽지 못한 이유). files는 base에서 읽은 {이름: 내용}이다."""
    for name in RUFF_CONFIG_FILES:
        if name not in files:
            continue
        if name != "pyproject.toml":
            return name, None
        try:
            data = tomllib.loads(files[name])
        except tomllib.TOMLDecodeError as e:
            return None, f"`pyproject.toml`을 읽지 못해 ruff 설정을 쓰지 않음: {e}"
        tool = data.get("tool")
        return (name if isinstance(tool, dict) and RUFF in tool else None), None
    return None, None


def pinned_version(files: Mapping[str, str]) -> tuple[str, str] | None:
    """(버전, 고정한 파일). pyproject.toml의 의존성 목록을 먼저, 그다음 requirements*.txt를 이름
    순서로 본다. 처음 찾은 정확한 고정(`==`)을 쓴다."""
    names = [n for n in ("pyproject.toml",) if n in files]
    names += sorted(n for n in files if REQUIREMENTS.match(n))
    for name in names:
        lines = (
            _pyproject_requirements(files[name])
            if name == "pyproject.toml"
            else (files[name].splitlines())
        )
        for line in lines:
            match = _PIN.match(line)
            if match:
                return match.group(1), name
    return None


def _pyproject_requirements(text: str) -> list[str]:
    """PEP 621의 `dependencies`, `optional-dependencies`와 PEP 735의 `dependency-groups`."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    out: list[str] = []
    project = data.get("project")
    if isinstance(project, dict):
        out += _strings(project.get("dependencies"))
        optional = project.get("optional-dependencies")
        if isinstance(optional, dict):
            for group in optional.values():
                out += _strings(group)
    groups = data.get("dependency-groups")
    if isinstance(groups, dict):
        for group in groups.values():
            out += _strings(group)
    return out


def _strings(value: object) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def config_rule(config: RuffConfig) -> Rule:
    """도구 설정에서 온 규칙 묶음 (4.3). 팀이 `ignore`, `per-file-ignores`, `# noqa`로 끈 것은
    ruff가 설정대로 따른다. 수정 방법은 ruff의 안전한 수정이 있는 위반만 기계적 수정이다."""
    section = "[tool.ruff]" if config.name == "pyproject.toml" else ""
    return Rule(
        id=CONFIG_RULE_ID,
        text=f"{config.name}의 설정대로 ruff를 통과한다",
        source=RuleSource(config.name, section, config.blob_sha),
        origin="tool_config",
        scope=(),
        check=CheckMethod("tool", tool=RUFF),
        fix="mechanical",
    )


def valid_codes(codes: Iterable[str]) -> tuple[str, ...] | None:
    """모델이 고른 ruff 코드의 형식. 틀리면 None. 실재하는지는 샌드박스의 `ruff rule`이 본다."""
    out = tuple(dict.fromkeys(c.strip().upper() for c in codes))
    if not out or len(out) > MAX_CODES or not all(RUFF_CODE.match(c) for c in out):
        return None
    return out
