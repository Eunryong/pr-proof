import pytest

from app.models.repo_config import MIN_MEMORY_GB, memory_limit, parse_repo_config


def test_missing_file_uses_defaults() -> None:
    config, problem = parse_repo_config(None)
    assert config.sandbox.memory_gb == 1 and problem is None


def test_empty_file_uses_defaults() -> None:
    assert parse_repo_config("")[1] is None


def test_reads_memory_and_ignores_other_sections() -> None:
    text = "sandbox:\n  memory_gb: 2\nskills:\n  enabled: [lint]\nmodel:\n  light: haiku\n"
    config, problem = parse_repo_config(text)
    assert config.sandbox.memory_gb == 2 and problem is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("sandbox: [", "YAML 형식 오류"),
        ("- a\n- b\n", "최상위가 매핑이 아님"),
        ("sandbox:\n  memory_gb: lots\n", "`sandbox.memory_gb` 값 오류"),
        (f"sandbox:\n  memory_gb: {MIN_MEMORY_GB / 2}\n", "`sandbox.memory_gb` 값 오류"),
    ],
    ids=["yaml", "not-mapping", "type", "too-small"],
)
def test_invalid_config_falls_back_with_reason(text: str, expected: str) -> None:
    config, problem = parse_repo_config(text)
    assert config.sandbox.memory_gb == 1  # 작업을 멈추지 않고 기본값으로 검사한다
    assert problem is not None and expected in problem


def test_minimum_memory_is_accepted() -> None:
    config, problem = parse_repo_config(f"sandbox:\n  memory_gb: {MIN_MEMORY_GB}\n")
    assert config.sandbox.memory_gb == MIN_MEMORY_GB and problem is None


@pytest.mark.parametrize(("requested", "expected"), [(2, 2), (4, 4), (8, 4)])
def test_memory_limit_is_capped(requested: float, expected: float) -> None:
    assert memory_limit(requested, 4) == expected


def test_skills_enabled_default_and_lint_off() -> None:
    assert "lint" in parse_repo_config(None)[0].skills.enabled
    config, problem = parse_repo_config("skills:\n  enabled: []\n")
    assert config.skills.enabled == [] and problem is None


def test_unknown_skill_is_config_problem() -> None:
    config, problem = parse_repo_config("skills:\n  enabled: [lint, typo]\n")
    assert "lint" in config.skills.enabled  # 기본값으로 검사한다
    assert problem is not None and "skills.enabled" in problem


def test_stage3_defaults() -> None:
    config, problem = parse_repo_config(None)
    assert problem is None
    assert (config.skills.fix.require_covered, config.skills.fix.max_retries) == (True, 3)
    assert config.skills.fix.require_approval is False  # 기본: 검사 뒤 바로 수정안까지 간다
    assert config.skills.static.min_repeats is None
    assert (config.model.light, config.model.heavy, config.model.budget_per_pr_usd) == (
        "haiku", "sonnet", 0.5,
    )  # fmt: skip


def test_stage3_values_are_read() -> None:
    text = (
        "skills:\n  fix: {require_covered: false, max_retries: 5}\n  static: {min_repeats: 2}\n"
        "model: {light: claude-haiku-5-5, heavy: opus, budget_per_pr_usd: 0}\n"
    )
    config, problem = parse_repo_config(text)
    assert problem is None
    assert config.skills.fix.require_covered is False and config.skills.fix.max_retries == 5
    assert config.skills.static.min_repeats == 2
    assert (config.model.light, config.model.budget_per_pr_usd) == ("claude-haiku-5-5", 0)


@pytest.mark.parametrize(
    ("text", "where"),
    [
        ("skills: {static: {min_repeats: 1}}", "skills.static.min_repeats"),
        ("skills: {fix: {max_retries: 0}}", "skills.fix.max_retries"),
        ("skills: {fix: {max_retries: 6}}", "skills.fix.max_retries"),  # 회차마다 테스트를 돈다
        ("model: {light: '--dangerously-skip-permissions'}", "model.light"),  # 옵션으로 읽힘
        ("model: {heavy: 'a b'}", "model.heavy"),
        ("model: {budget_per_pr_usd: -1}", "model.budget_per_pr_usd"),
    ],
)
def test_stage3_bad_values_fall_back_to_defaults(text: str, where: str) -> None:
    config, problem = parse_repo_config(text)
    assert problem is not None and f"`{where}`" in problem
    assert config.model.light == "haiku" and config.skills.fix.max_retries == 3
