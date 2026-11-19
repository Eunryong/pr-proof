"""대상 리포의 `.pipeline.yaml` (base 브랜치에서 읽음, docs/DESIGN.md 설정).

지금 단계에서 쓰는 항목만 정의한다. 다른 항목은 무시한다 (스키마 초안의 나머지는 그 단계에서).
"""

from collections.abc import Mapping
from typing import Annotated, Final

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

# docker가 받는 최소 메모리보다 넉넉히 크게. 너무 작으면 컨테이너가 뜨지 않아 환경 오류로 보인다
MIN_MEMORY_GB: Final = 0.5
DEFAULT_MAX_RETRIES: Final = 3
# 회차마다 테스트를 다시 돌리므로 리포 설정으로 무한히 늘리지 못하게. 넘는 값은 이 값으로 줄인다
MAX_RETRIES_CAP: Final = 5
# claude --model에 넘기는 이름. `-`로 시작하면 옵션으로 읽히므로 막는다
MODEL_NAME_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9._\[\]-]*$"
DEFAULT_RULE_DOCS: Final = ("README.md", "CONTRIBUTING.md")
LINT: Final = "lint"
# 2단계에 없앤 설정 (10절 "v1 스키마에서 바뀐 키"). 있으면 무시하고 "설정:"에 알린다
REMOVED_KEYS: Final[Mapping[tuple[str, str], str]] = {
    ("skills", "static"): "`skills.static`은 없어졌습니다 (파이프라인 내장 검사기가 없음)",
    ("model", "budget_per_pr_usd"): (
        "`model.budget_per_pr_usd`는 없어졌습니다. 호출 수 상한은 `model.calls`입니다"
    ),
}
MAX_RULE_DOCS: Final = 20


def _check_doc_path(path: str) -> str:
    """루트 기준 상대 경로만. git 인자와 문서 경계 표시에 그대로 들어가므로 이상한 글자를 막는다."""
    parts = path.split("/")
    if (
        not path
        or path.startswith(("/", "-"))
        or any(part in ("", ".", "..") for part in parts)
        or any(not ch.isprintable() or ch in '\\"' for ch in path)
    ):
        raise ValueError(f"루트 기준 상대 경로가 아님: {path!r}")
    return path


DocPath = Annotated[str, AfterValidator(_check_doc_path)]
RuleId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class RulesSettings(BaseModel):
    """규칙 문서와 끈 규칙 (DESIGN.md 4.3, 10절 `rules`). docs를 적으면 기본 목록을 대신한다."""

    model_config = ConfigDict(extra="ignore")
    docs: list[DocPath] = Field(
        default_factory=lambda: list(DEFAULT_RULE_DOCS), max_length=MAX_RULE_DOCS
    )
    disabled: list[RuleId] = Field(default_factory=list)


class SandboxSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    memory_gb: float = Field(default=1, ge=MIN_MEMORY_GB)


class FixSettings(BaseModel):
    """수정 설정 (DESIGN.md 10절 `fix`). 파이프라인 쪽 최댓값은 `fix_limits`가 적용한다."""

    model_config = ConfigDict(extra="ignore")
    require_approval: bool = False  # 검사 뒤 바로 수정안을 만든다. true면 /fix 승인 후 만든다
    # 모델 수정은 테스트가 지나가는 위반 줄만 한다 (4.6)
    require_covered: bool = True
    # 모델 수정의 작성 → 검증 회차 상한 (4.7)
    max_retries: int = Field(default=DEFAULT_MAX_RETRIES, ge=1)


class CallSettings(BaseModel):
    """모델 호출 수 상한 (docs/DESIGN.md 9.2, 10절 `model.calls`). 판단은 models/call_budget.py.

    `pr_base`·`per_violation`은 위반 수에 비례하는 PR 상한을 넣는 4단계에 읽는다. 그때까지
    PR 상한은 `pr_max`다.
    """

    model_config = ConfigDict(extra="ignore")
    pr_max: int = Field(default=150, ge=0)
    ruleset_base: int = Field(default=10, ge=0)
    per_rule: int = Field(default=4, ge=0)
    ruleset_max: int = Field(default=100, ge=0)
    repo_daily: int = Field(default=500, ge=0)


class ModelSettings(BaseModel):
    """모델과 호출 상한. 백엔드는 claude CLI만 있다."""

    model_config = ConfigDict(extra="ignore")
    # 규칙 읽기와 검사기 작성의 모델 (11.2). 검사기 작성은 실패해도 더 높은 모델로 올리지 않는다:
    # 3회 안에 안 되면 "검사하지 못한 규칙"으로 두고 4단계의 모델 판단으로 넘긴다 (11.2 D7)
    rules: str = Field(default="sonnet", pattern=MODEL_NAME_PATTERN)
    checker: str = Field(default="sonnet", pattern=MODEL_NAME_PATTERN)
    decide: str = Field(default="sonnet", pattern=MODEL_NAME_PATTERN)  # 수정 판단 (4.6)
    # 수정 경로의 모델: 가벼운 모델로 시작하고 실패가 이어지면 무거운 모델로 한 번 올린다
    light: str = Field(default="haiku", pattern=MODEL_NAME_PATTERN)
    heavy: str = Field(default="sonnet", pattern=MODEL_NAME_PATTERN)
    calls: CallSettings = CallSettings()


class SkillsSettings(BaseModel):
    """v1의 스킬 설정. 남은 스킬은 lint 하나이고 3단계에 도구 규칙으로 옮긴다 (10절)."""

    model_config = ConfigDict(extra="ignore")
    # lint를 빼면 v1 lint 경로가 돌지 않는다. 옛 이름(static, structure)은 읽기만 하고 쓰지 않는다
    enabled: list[str] = Field(default_factory=lambda: [LINT])


class RepoConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    rules: RulesSettings = RulesSettings()
    sandbox: SandboxSettings = SandboxSettings()
    skills: SkillsSettings = SkillsSettings()
    model: ModelSettings = ModelSettings()
    fix: FixSettings = FixSettings()

    @model_validator(mode="before")
    @classmethod
    def _legacy_fix_key(cls, data: object) -> object:
        """구현 2단계 동안 옛 키 `skills.fix`도 읽는다 (10절). 새 키 `fix`가 있으면 그것만 쓴다."""
        if isinstance(data, dict) and "fix" not in data:
            skills = data.get("skills")
            if isinstance(skills, dict) and "fix" in skills:
                return {**data, "fix": skills["fix"]}
        return data


def parse_repo_config(text: str | None) -> tuple[RepoConfig, str | None]:
    """(설정, 문제). 파일이 없으면 기본값. 읽지 못하면 기본값과 그 이유를 돌려준다.

    설정 오류로 작업을 멈추지 않는다. 기본값으로 검사하고 코멘트에 이유를 적는다.
    """
    if text is None:
        return RepoConfig(), None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return RepoConfig(), f"YAML 형식 오류 ({_first_line(str(e))})"
    if data is None:
        return RepoConfig(), None
    if not isinstance(data, dict):
        return RepoConfig(), "최상위가 매핑이 아님"
    try:
        return RepoConfig.model_validate(data), None
    except ValidationError as e:
        errors = e.errors()
        where = ".".join(str(p) for p in errors[0]["loc"])
        return RepoConfig(), f"`{where}` 값 오류 ({errors[0]['msg']})"


def settings_notes(text: str | None) -> list[str]:
    """코멘트의 "설정:"에 알릴 옛 키 (10절 "v1 스키마에서 바뀐 키"). 읽지 못하는 파일은 빈 목록이다.

    읽지 못한 이유는 `parse_repo_config`가 따로 알린다.
    """
    try:
        data = yaml.safe_load(text) if text else None
    except yaml.YAMLError:
        return []
    if not isinstance(data, dict):
        return []
    notes = []
    skills = data.get("skills")
    if isinstance(skills, dict) and "fix" in skills:
        notes.append(
            "`skills.fix`는 `fix`로 바뀌었습니다. "
            "2단계 동안은 `fix`가 없으면 `skills.fix`를 읽습니다"
        )
    for (section, key), note in REMOVED_KEYS.items():
        part = data.get(section)
        if isinstance(part, dict) and key in part:
            notes.append(note)
    enabled = skills.get("enabled") if isinstance(skills, dict) else None
    gone = [s for s in enabled if s != LINT] if isinstance(enabled, list) else []
    if gone:
        names = ", ".join(f"`{s}`" for s in gone)
        notes.append(f"`skills.enabled`에 남은 스킬은 `lint`뿐이라 {names}는 무시합니다")
    return notes


def fix_limits(requested: FixSettings, *, max_retries: int = MAX_RETRIES_CAP) -> FixSettings:
    """리포가 요청한 수정 설정. 파이프라인 쪽 최댓값을 넘는 값은 그 값으로 줄인다 (9.2, T23)."""
    return requested.model_copy(update={"max_retries": min(requested.max_retries, max_retries)})


def memory_limit(requested_gb: float, max_gb: float) -> float:
    """리포가 요청한 메모리 한도. 파이프라인 쪽 상한을 넘으면 상한으로 줄인다."""
    return min(requested_gb, max_gb)


def call_limits(
    requested: CallSettings, *, max_pr_max: int, max_ruleset_max: int, max_repo_daily: int
) -> CallSettings:
    """리포가 요청한 호출 상한. 파이프라인 쪽 최댓값을 넘는 값은 그 값으로 줄인다 (9.2)."""
    return requested.model_copy(
        update={
            "pr_max": min(requested.pr_max, max_pr_max),
            "ruleset_max": min(requested.ruleset_max, max_ruleset_max),
            "repo_daily": min(requested.repo_daily, max_repo_daily),
        }
    )


def _first_line(text: str) -> str:
    return text.strip().splitlines()[0] if text.strip() else text
