"""base 브랜치에서 대상 리포의 설정을 읽는다: `.pipeline.yaml`과 ruff 설정 (docs/DESIGN.md 설정).

ruff 설정 파일은 worktree의 `.pipeline-out/base/` 아래로 꺼내 샌드박스의 ruff에 `--config`로 넘긴다.
PR head에서 바꾼 설정은 쓰지 않는다.
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.models.repo_config import (
    RepoConfig,
    fix_limits,
    memory_limit,
    parse_repo_config,
    settings_notes,
)
from app.sandbox.container import OUT_DIR
from app.worker import repo

PIPELINE_YAML: Final = ".pipeline.yaml"
# ruff가 설정 파일을 찾는 우선순위와 같다
RUFF_CONFIG_FILES: Final = (".ruff.toml", "ruff.toml", "pyproject.toml")
BASE_DIR: Final = f"{OUT_DIR}/base"
RUFF_DEFAULT_SOURCE: Final = "기본 규칙"  # 코멘트에 "ruff (기본 규칙)"으로 나온다


@dataclass(frozen=True)
class BaseConfig:
    repo: RepoConfig  # `fix`는 파이프라인 쪽 최댓값을 적용한 값이다
    memory_gb: float  # 파이프라인 상한을 적용한 실제 한도
    max_memory_gb: float
    ruff_config: str | None  # worktree 기준 경로. None이면 ruff 기본 규칙
    ruff_source: str  # 코멘트에 적는 규칙 출처
    problems: tuple[str, ...] = ()  # 읽지 못했거나 바꿔 읽은 설정. 코멘트의 "설정:"에 적는다


def load(worktree: Path, base_sha: str, max_memory_gb: float) -> BaseConfig:
    files = repo.read_base_files(worktree, base_sha, (PIPELINE_YAML, *RUFF_CONFIG_FILES))
    problems: list[str] = []

    repo_config, problem = parse_repo_config(files.get(PIPELINE_YAML))
    if problem:
        problems.append(f"`{PIPELINE_YAML}`을 읽지 못해 기본값을 씀: {problem}")
    problems += settings_notes(files.get(PIPELINE_YAML))
    fix = fix_limits(repo_config.fix)
    if fix != repo_config.fix:
        problems.append(
            f"`fix.max_retries` {repo_config.fix.max_retries}는 파이프라인 최댓값 "
            f"{fix.max_retries}로 줄였습니다"
        )
        repo_config = repo_config.model_copy(update={"fix": fix})

    ruff_config, ruff_source = None, RUFF_DEFAULT_SOURCE
    name = _ruff_config_name(files, problems)
    if name is not None:
        out = worktree / BASE_DIR / name
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(files[name])
        ruff_config, ruff_source = f"{BASE_DIR}/{name}", name

    return BaseConfig(
        repo=repo_config,
        memory_gb=memory_limit(repo_config.sandbox.memory_gb, max_memory_gb),
        max_memory_gb=max_memory_gb,
        ruff_config=ruff_config,
        ruff_source=ruff_source,
        problems=tuple(problems),
    )


def _ruff_config_name(files: dict[str, str], problems: list[str]) -> str | None:
    """쓸 ruff 설정 파일 이름. pyproject.toml은 [tool.ruff]가 있을 때만 ruff 설정으로 본다."""
    for name in RUFF_CONFIG_FILES:
        if name not in files:
            continue
        if name != "pyproject.toml":
            return name
        try:
            data = tomllib.loads(files[name])
        except tomllib.TOMLDecodeError as e:
            problems.append(f"`pyproject.toml`을 읽지 못해 ruff 기본 규칙을 씀: {e}")
            return None
        return name if "ruff" in data.get("tool", {}) else None
    return None
