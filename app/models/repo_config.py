"""대상 리포의 `.pipeline.yaml` (base 브랜치에서 읽음, docs/DESIGN.md 설정).

지금 단계에서 쓰는 항목만 정의한다. 다른 항목은 무시한다 (스키마 초안의 나머지는 그 단계에서).
"""

from typing import Final

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.models.finding import Skill

# docker가 받는 최소 메모리보다 넉넉히 크게. 너무 작으면 컨테이너가 뜨지 않아 환경 오류로 보인다
MIN_MEMORY_GB: Final = 0.5
DEFAULT_MAX_RETRIES: Final = 3
MAX_RETRIES_CAP: Final = 5  # 회차마다 테스트를 다시 돌리므로 리포 설정으로 무한히 늘리지 못하게
# claude --model에 넘기는 이름. `-`로 시작하면 옵션으로 읽히므로 막는다
MODEL_NAME_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9._\[\]-]*$"


class SandboxSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    memory_gb: float = Field(default=1, ge=MIN_MEMORY_GB)


class FixSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    require_approval: bool = False  # 검사 뒤 바로 수정안을 만든다. true면 /fix 승인 후 만든다
    # static·structure는 테스트가 지나가는 위반 줄만 자동 수정한다 (DESIGN.md F8)
    require_covered: bool = True
    # AI 수정의 작성 → 검증 회차 상한 (DESIGN.md F7)
    max_retries: int = Field(default=DEFAULT_MAX_RETRIES, ge=1, le=MAX_RETRIES_CAP)


class StaticSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    # 같은 리터럴이 이 횟수 이상 반복되면 위반. None이면 기본값(3)이고 출처는 "기본 규칙"
    min_repeats: int | None = Field(default=None, ge=2)


class ModelSettings(BaseModel):
    """AI 수정에 쓸 모델과 PR당 예산 (DESIGN.md 비용 최적화). 백엔드는 claude CLI만 있다."""

    model_config = ConfigDict(extra="ignore")
    light: str = Field(default="haiku", pattern=MODEL_NAME_PATTERN)
    heavy: str = Field(default="sonnet", pattern=MODEL_NAME_PATTERN)
    budget_per_pr_usd: float = Field(default=0.5, ge=0)


class SkillsSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    # 켤 규칙 스킬. lint를 빼면 "테스트만" 돈다. static·structure는 3·4단계에서 붙는다
    enabled: list[Skill] = Field(default_factory=lambda: ["lint", "static", "structure"])
    fix: FixSettings = FixSettings()
    static: StaticSettings = StaticSettings()


class ExtensionSettings(BaseModel):
    """선택 확장 하나 (7단계). 지금은 꺼져 있다는 안내에만 쓴다 (DESIGN.md F11)."""

    model_config = ConfigDict(extra="ignore")
    enabled: bool = False


class Extensions(BaseModel):
    model_config = ConfigDict(extra="ignore")
    mutation: ExtensionSettings = ExtensionSettings()
    ai_tests: ExtensionSettings = ExtensionSettings()


class RepoConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    sandbox: SandboxSettings = SandboxSettings()
    skills: SkillsSettings = SkillsSettings()
    model: ModelSettings = ModelSettings()
    extensions: Extensions = Extensions()

    def extension_enabled(self, name: str) -> bool:
        settings = getattr(self.extensions, name, None)
        return bool(settings and settings.enabled)


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


def memory_limit(requested_gb: float, max_gb: float) -> float:
    """리포가 요청한 메모리 한도. 파이프라인 쪽 상한을 넘으면 상한으로 줄인다."""
    return min(requested_gb, max_gb)


def _first_line(text: str) -> str:
    return text.strip().splitlines()[0] if text.strip() else text
