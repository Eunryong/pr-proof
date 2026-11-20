import pytest

from app.models.repo_config import (
    MIN_MEMORY_GB,
    CallSettings,
    call_limits,
    memory_limit,
    parse_repo_config,
)


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


def test_decision_model_is_its_own_setting() -> None:
    default, _ = parse_repo_config(None)
    assert default.model.decide == "sonnet"
    config, problem = parse_repo_config("model: {decide: opus}\n")
    assert problem is None and (config.model.decide, config.model.checker) == ("opus", "sonnet")
    _, problem = parse_repo_config("model: {decide: '-x'}\n")
    assert problem is not None


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


def test_call_limits_default_and_parsed() -> None:
    calls = parse_repo_config(None)[0].model.calls
    assert (calls.pr_max, calls.ruleset_base, calls.per_rule, calls.ruleset_max) == (
        150,
        10,
        4,
        100,
    )
    assert calls.repo_daily == 500
    config, problem = parse_repo_config("model:\n  calls:\n    pr_max: 3\n    repo_daily: 0\n")
    assert (config.model.calls.pr_max, config.model.calls.repo_daily, problem) == (3, 0, None)


def test_negative_call_limit_falls_back_with_reason() -> None:
    config, problem = parse_repo_config("model:\n  calls:\n    ruleset_max: -1\n")
    assert config.model.calls.ruleset_max == 100
    assert problem is not None and "model.calls.ruleset_max" in problem


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ((500, 300, 2000), (500, 300, 2000)),
        ((501, 301, 2001), (500, 300, 2000)),
        ((1, 2, 3), (1, 2, 3)),
    ],
)
def test_call_limits_are_capped_by_pipeline(requested: tuple, expected: tuple) -> None:
    pr_max, ruleset_max, repo_daily = requested
    capped = call_limits(
        CallSettings(pr_max=pr_max, ruleset_max=ruleset_max, repo_daily=repo_daily),
        max_pr_max=500, max_ruleset_max=300, max_repo_daily=2000,
    )  # fmt: skip
    assert (capped.pr_max, capped.ruleset_max, capped.repo_daily) == expected


def test_rules_defaults_and_docs_replace_the_default_list() -> None:
    rules = parse_repo_config(None)[0].rules
    assert (rules.docs, rules.disabled) == (["README.md", "CONTRIBUTING.md"], [])
    text = "rules:\n  docs: [docs/rules.md]\n  disabled: [R-readme-3fa9c2d1, ' PLR2004 ']\n"
    config, problem = parse_repo_config(text)
    assert config.rules.docs == ["docs/rules.md"] and problem is None
    assert config.rules.disabled == ["R-readme-3fa9c2d1", "PLR2004"]
    assert parse_repo_config("rules:\n  docs: []\n")[0].rules.docs == []


@pytest.mark.parametrize(
    "path", ["../x.md", "/etc/passwd", "-x.md", "a//b.md", "./a.md", "a\\\\b.md", "a\\tb.md", ""]
)
def test_bad_rule_doc_path_falls_back_with_reason(path: str) -> None:
    config, problem = parse_repo_config(f'rules:\n  docs: ["{path}"]\n')
    assert config.rules.docs == ["README.md", "CONTRIBUTING.md"]
    assert problem is not None and "rules.docs" in problem


def test_too_many_rule_docs_and_empty_disabled_id_are_problems() -> None:
    many = ", ".join(f"d{i}.md" for i in range(21))
    assert "rules.docs" in parse_repo_config(f"rules:\n  docs: [{many}]\n")[1]
    assert "rules.disabled" in parse_repo_config("rules:\n  disabled: ['  ']\n")[1]
