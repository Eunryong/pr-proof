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
    field_validator,
    model_validator,
)

from app.worker.rag_switch_pure import DEFAULT_MODE, MODES, RagMode, parse_mode

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
# 2026-11-21에 없앤 키 (11.1). 옛 위치 `skills.fix` 아래에 있어도 알린다
REQUIRE_COVERED_NOTE: Final = (
    "`fix.require_covered`는 없어졌습니다. 테스트가 실행하지 않는 줄의 위반도 고치고, "
    "수정안 PR에 그 사실을 적습니다"
)
MAX_RULE_DOCS: Final = 20
# 유사도 기준값의 기본. 6.7의 측정으로 정했다 (multilingual-e5-small, 2026-11-20, 11.1)
DEFAULT_MIN_SIMILARITY: Final = 0.91
# 프롬프트에 넣는 기록 수의 상한. 저장소 설정이 프롬프트를 끝없이 늘리지 못하게
MAX_RECORDS_CAP: Final = 10


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
    # 모델 수정의 작성 → 검증 회차 상한 (4.7)
    max_retries: int = Field(default=DEFAULT_MAX_RETRIES, ge=1)


class CallSettings(BaseModel):
    """모델 호출 수 상한 (docs/DESIGN.md 9.2, 10절 `model.calls`). 판단은
    app/worker/call_budget_pure.py."""

    model_config = ConfigDict(extra="ignore")
    pr_base: int = Field(default=20, ge=0)
    per_violation: int = Field(default=4, ge=0)
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
    judge: str = Field(default="sonnet", pattern=MODEL_NAME_PATTERN)  # 모델의 직접 판단 (4.5)
    # 수정 경로의 모델: 가벼운 모델로 시작하고 실패가 이어지면 무거운 모델로 한 번 올린다
    light: str = Field(default="haiku", pattern=MODEL_NAME_PATTERN)
    heavy: str = Field(default="sonnet", pattern=MODEL_NAME_PATTERN)
    calls: CallSettings = CallSettings()


class SkillsSettings(BaseModel):
    """v1의 스킬 설정. 3단계에 v1 lint를 도구 규칙으로 옮겨 스킬이 없어졌다 (10절). 옛 파일을 읽기만
    한다: `enabled`에 lint가 없으면 그 저장소는 ruff를 끈 것이라 도구 설정의 규칙 묶음을 끈다
    (app/worker/base_config.py)."""

    model_config = ConfigDict(extra="ignore")
    enabled: list[str] | None = None


class RagSettings(BaseModel):
    """과거 기록 검색 (DESIGN.md 6.4–6.6, 10절 `rag`). 기록 저장은 이 값과 관계없이 한다."""

    model_config = ConfigDict(extra="ignore")
    # 검색 결과를 프롬프트에 넣을지. auto는 기록 수와 채택률로 정한다
    # (app/worker/rag_switch_pure.py)
    enabled: RagMode = DEFAULT_MODE
    top_k: int = Field(default=20, ge=1, le=200)  # 검색이 가져오는 후보 수
    # 이 유사도 미만의 기록은 넣지 않는다. recall@k와 관련 없는 짝의 분포로 정한 값 (6.7)
    min_similarity: float = Field(default=DEFAULT_MIN_SIMILARITY, ge=-1, le=1)
    # 한 호출의 프롬프트에 넣는 기록 수 상한. top_k와 따로 둔다
    max_records: int = Field(default=3, ge=0, le=MAX_RECORDS_CAP)

    @field_validator("enabled", mode="before")
    @classmethod
    def _mode(cls, value: object) -> RagMode:
        """옛 불리언도 읽는다. 모르는 값은 설정 전체를 버리지 않고 이 값만 기본값으로 읽는다
        ("설정:"의 알림은 `settings_notes`가 단다)."""
        return parse_mode(value) or DEFAULT_MODE


class RepoConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    rules: RulesSettings = RulesSettings()
    sandbox: SandboxSettings = SandboxSettings()
    skills: SkillsSettings = SkillsSettings()
    model: ModelSettings = ModelSettings()
    fix: FixSettings = FixSettings()
    rag: RagSettings = RagSettings()

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
    fix_parts = [data.get("fix"), skills.get("fix") if isinstance(skills, dict) else None]
    if any(isinstance(part, dict) and "require_covered" in part for part in fix_parts):
        notes.append(REQUIRE_COVERED_NOTE)
    rag = data.get("rag")
    if isinstance(rag, dict) and "enabled" in rag and parse_mode(rag["enabled"]) is None:
        # 받은 값은 적지 않는다. 저장소의 글이 코멘트에 그대로 들어가지 않게
        notes.append(
            f"`rag.enabled`는 {', '.join(MODES)} 중 하나여야 합니다. 기본값 {DEFAULT_MODE}로 "
            "읽었습니다"
        )
    enabled = skills.get("enabled") if isinstance(skills, dict) else None
    if isinstance(enabled, list) and LINT in enabled:
        notes.append(
            "`skills.enabled`는 없어졌습니다. 규칙은 문서와 도구 설정에서 읽고, 끌 규칙은 "
            "`rules.disabled`에 적습니다"
        )
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
