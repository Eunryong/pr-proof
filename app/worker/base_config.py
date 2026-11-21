"""base 브랜치에서 대상 리포의 설정을 읽는다: `.pipeline.yaml`, ruff 설정, ruff 버전 고정
(docs/DESIGN.md 4.3 도구 설정, 4.4 도구 준비, 8.3, 10절).

PR head에서 바꾼 설정은 쓰지 않는다. 저장소 루트의 일반 파일만 읽는다: 심볼릭 링크(120000), 폴더,
서브모듈인 설정은 읽지 않고 "설정:"에 알린다 (T24). ruff 설정 파일은 worktree에 꺼내지 않고 내용만
들고 있다가 샌드박스의 ruff에 검사할 폴더 밖의 사본으로 넘긴다 (app/worker/lang/python.py).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.worker import repo
from app.worker.access_pure import AccessSettings, parse_access
from app.worker.repo_config_pure import (
    LINT,
    RepoConfig,
    fix_limits,
    memory_limit,
    parse_repo_config,
    settings_notes,
)
from app.worker.rule_reading_pure import REGULAR_FILE_MODES
from app.worker.sandbox import toolcache
from app.worker.tools_pure import (
    CONFIG_RULE_ID,
    REQUIREMENTS,
    RUFF,
    RUFF_CONFIG_FILES,
    RuffConfig,
    RuffSetup,
    pinned_version,
    ruff_config_name,
)

PIPELINE_YAML: Final = ".pipeline.yaml"
MAX_SETTINGS_BYTES: Final = 1024 * 1024


@dataclass(frozen=True)
class BaseConfig:
    repo: RepoConfig  # `fix`는 파이프라인 쪽 최댓값을 적용한 값이다
    memory_gb: float  # 파이프라인 상한을 적용한 실제 한도
    max_memory_gb: float
    ruff: RuffSetup  # 이 저장소의 ruff 버전과 설정. 받는 것은 규칙이 ruff를 쓸 때뿐이다
    problems: tuple[str, ...] = ()  # 읽지 못했거나 바꿔 읽은 설정. 코멘트의 "설정:"에 적는다


def _wanted(name: str) -> bool:
    return name in (PIPELINE_YAML, *RUFF_CONFIG_FILES) or bool(REQUIREMENTS.match(name))


def _read_settings(
    worktree: Path, base_sha: str
) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """({이름: 내용}, {이름: blob 해시}, 문제). 일반 파일이 아니거나 너무 크거나 글이 아니면
    읽지 않는다."""
    files: dict[str, str] = {}
    blobs: dict[str, str] = {}
    problems: list[str] = []
    for name, entry in sorted(repo.base_root_entries(worktree, base_sha).items()):
        if not _wanted(name):
            continue
        if entry.mode not in REGULAR_FILE_MODES or entry.size is None:
            problems.append(f"`{name}`이 일반 파일이 아니라 읽지 않음 (심볼릭 링크, 폴더 등)")
            continue
        if entry.size > MAX_SETTINGS_BYTES:
            problems.append(f"`{name}`이 {MAX_SETTINGS_BYTES // 1024}KB를 넘어 읽지 않음")
            continue
        try:
            files[name] = repo.read_blob(worktree, entry.sha)
        except UnicodeDecodeError:
            problems.append(f"`{name}`이 UTF-8 글이 아니라 읽지 않음")
            continue
        blobs[name] = entry.sha
    return files, blobs, problems


def load_access(
    data_dir: Path, repo_name: str, base_sha: str, *, url: str | None = None
) -> AccessSettings:
    """외부 작성자 PR 가드레일의 `access` (8.6). PR의 코드를 체크아웃하기 전에 읽는다.

    worktree를 만들지 않고 bare 캐시에서 base 커밋의 `.pipeline.yaml` blob만 읽는다. 일반 파일이
    아니거나 너무 크거나 글이 아니면 기본값이다. 문제는 검사가 돌 때 `load`가 "설정:"에 알린다.
    git이 실패하면 GitError를 올려 실행하지 않는다.
    """
    bare = repo.ensure_bare(data_dir, repo_name, url)
    repo.fetch_commit(bare, base_sha)
    entry = repo.bare_entries(bare, base_sha, [PIPELINE_YAML]).get(PIPELINE_YAML)
    if (
        entry is None
        or entry.mode not in REGULAR_FILE_MODES
        or entry.size is None
        or entry.size > MAX_SETTINGS_BYTES
    ):
        return AccessSettings()
    try:
        text = repo.bare_blob(bare, entry.sha)
    except UnicodeDecodeError:
        return AccessSettings()
    return parse_access(text)[0]


def load(worktree: Path, base_sha: str, max_memory_gb: float) -> BaseConfig:
    files, blobs, problems = _read_settings(worktree, base_sha)

    repo_config, problem = parse_repo_config(files.get(PIPELINE_YAML))
    if problem:
        problems.append(f"`{PIPELINE_YAML}`을 읽지 못해 기본값을 씀: {problem}")
    problems += settings_notes(files.get(PIPELINE_YAML))
    problems += parse_access(files.get(PIPELINE_YAML))[1]
    fix = fix_limits(repo_config.fix)
    if fix != repo_config.fix:
        problems.append(
            f"`fix.max_retries` {repo_config.fix.max_retries}는 파이프라인 최댓값 "
            f"{fix.max_retries}로 줄였습니다"
        )
        repo_config = repo_config.model_copy(update={"fix": fix})
    repo_config = _legacy_lint_off(repo_config, problems)

    name, problem = ruff_config_name(files)
    if problem:
        problems.append(problem)
    config = RuffConfig(name, files[name], blobs[name]) if name is not None else None
    pinned = pinned_version(files)
    version, pinned_by = pinned if pinned else (toolcache.TOOLS[RUFF].default, "")
    ruff = RuffSetup(
        version=version, pinned_by=pinned_by, config=config,
        available=toolcache.is_known(RUFF, version),
    )  # fmt: skip
    if not ruff.available:
        problems.append(
            f"{ruff.label}은 파이프라인이 해시를 고정한 버전이 아니라 받지 않습니다. ruff를 쓰는 "
            "규칙은 검사하지 못합니다"
        )

    return BaseConfig(
        repo=repo_config,
        memory_gb=memory_limit(repo_config.sandbox.memory_gb, max_memory_gb),
        max_memory_gb=max_memory_gb,
        ruff=ruff,
        problems=tuple(problems),
    )


def _legacy_lint_off(config: RepoConfig, problems: list[str]) -> RepoConfig:
    """v1의 `skills.enabled`에서 lint를 뺀 저장소는 ruff를 끈 뜻이었다. 도구 설정의 규칙 묶음을
    `rules.disabled`로 끄고 새 키를 알린다 (10절). 키가 없으면 아무것도 하지 않는다."""
    enabled = config.skills.enabled
    if enabled is None or LINT in enabled or CONFIG_RULE_ID in config.rules.disabled:
        return config
    problems.append(
        f"`skills.enabled`에 `lint`가 없어 도구 설정의 ruff 규칙(`{CONFIG_RULE_ID}`)을 끕니다. "
        f"`skills`는 없어졌으니 `rules.disabled: [{CONFIG_RULE_ID}]`로 바꿔 주세요"
    )
    rules = config.rules.model_copy(update={"disabled": [*config.rules.disabled, CONFIG_RULE_ID]})
    return config.model_copy(update={"rules": rules})
