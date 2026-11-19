import pytest

from app.models.repo_config import (
    MAX_RETRIES_CAP,
    MIN_MEMORY_GB,
    CallSettings,
    FixSettings,
    call_limits,
    fix_limits,
    memory_limit,
    parse_repo_config,
    settings_notes,
)


def test_missing_file_uses_defaults() -> None:
    config, problem = parse_repo_config(None)
    assert config.sandbox.memory_gb == 1 and problem is None


def test_empty_file_uses_defaults() -> None:
    assert parse_repo_config("")[1] is None


def test_extension_settings_are_gone() -> None:
    """v1 F13의 확장 명령을 범위에서 뺐다 (10절). 옛 `extensions` 키는 모르는 키로 무시한다."""
    config, problem = parse_repo_config("extensions:\n  mutation:\n    enabled: true\n")
    assert problem is None and not hasattr(config, "extensions")


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


def test_removed_or_unknown_skill_is_ignored_with_a_note() -> None:
    """2단계에 static을 없앴다. 옛 이름이나 모르는 이름이 있어도 설정 전체를 기본값으로 되돌리지
    않고, 그 이름만 무시하고 "설정:"에 알린다 (10절)."""
    text = "skills:\n  enabled: [lint, static, structure]\nsandbox: {memory_gb: 2}\n"
    config, problem = parse_repo_config(text)
    assert problem is None and config.sandbox.memory_gb == 2 and "lint" in config.skills.enabled
    (note,) = settings_notes(text)
    assert "`static`, `structure`는 무시합니다" in note
    off = "skills:\n  enabled: [static]\n"
    assert "lint" not in parse_repo_config(off)[0].skills.enabled  # lint를 끈 것은 그대로다
    assert settings_notes("skills:\n  enabled: [lint]\n") == []


def test_removed_static_and_dollar_keys_are_ignored_with_a_note() -> None:
    """`skills.static.min_repeats`와 `model.budget_per_pr_usd`는 2단계에 없앴다. 남은 키는 그대로
    읽고, 틀린 값이어도 설정 오류가 되지 않는다."""
    text = (
        "skills:\n  static: {min_repeats: 1}\n"
        "model: {light: claude-haiku-5-5, budget_per_pr_usd: -1}\nfix: {max_retries: 2}\n"
    )
    config, problem = parse_repo_config(text)
    assert problem is None
    assert (config.model.light, config.fix.max_retries) == ("claude-haiku-5-5", 2)
    notes = settings_notes(text)
    assert len(notes) == 2
    assert any("`skills.static`은 없어졌습니다" in n for n in notes)
    assert any("`model.budget_per_pr_usd`는 없어졌습니다" in n for n in notes)


def test_stage3_defaults() -> None:
    config, problem = parse_repo_config(None)
    assert problem is None
    assert (config.fix.require_covered, config.fix.max_retries) == (True, 3)
    assert config.fix.require_approval is False  # 기본: 검사 뒤 바로 수정안까지 간다
    assert (config.model.light, config.model.heavy) == ("haiku", "sonnet")


def test_decision_model_is_its_own_setting() -> None:
    default, _ = parse_repo_config(None)
    assert default.model.decide == "sonnet"
    config, problem = parse_repo_config("model: {decide: opus}\n")
    assert problem is None and (config.model.decide, config.model.checker) == ("opus", "sonnet")
    _, problem = parse_repo_config("model: {decide: '-x'}\n")
    assert problem is not None


def test_stage3_values_are_read() -> None:
    text = (
        "fix: {require_covered: false, max_retries: 5, require_approval: true}\n"
        "model: {light: claude-haiku-5-5, heavy: opus}\n"
    )
    config, problem = parse_repo_config(text)
    assert problem is None and settings_notes(text) == []
    assert config.fix == FixSettings(require_approval=True, require_covered=False, max_retries=5)
    assert (config.model.light, config.model.heavy) == ("claude-haiku-5-5", "opus")


def test_old_skills_fix_key_is_read_and_noted_during_stage_2() -> None:
    """10절: `skills.fix.*`는 `fix.*`가 되었다. 2단계 동안 옛 키도 읽고 "설정:"에 새 키를 알린다."""
    old = "skills:\n  fix: {require_approval: true, max_retries: 2}\n"
    config, problem = parse_repo_config(old)
    assert problem is None and config.fix.require_approval and config.fix.max_retries == 2
    (note,) = settings_notes(old)
    assert "`skills.fix`는 `fix`로 바뀌었습니다" in note
    both = "fix: {max_retries: 4}\nskills:\n  fix: {max_retries: 2, require_approval: true}\n"
    config, _ = parse_repo_config(both)
    assert config.fix == FixSettings(max_retries=4)  # 새 키가 있으면 옛 키는 읽지 않는다
    assert settings_notes(None) == [] and settings_notes("fix: [") == []


@pytest.mark.parametrize(
    ("text", "where"),
    [
        ("fix: {max_retries: 0}", "fix.max_retries"),
        ("skills: {fix: {max_retries: 0}}", "fix.max_retries"),
        ("model: {light: '--dangerously-skip-permissions'}", "model.light"),  # 옵션으로 읽힘
        ("model: {heavy: 'a b'}", "model.heavy"),
    ],
)
def test_stage3_bad_values_fall_back_to_defaults(text: str, where: str) -> None:
    config, problem = parse_repo_config(text)
    assert problem is not None and f"`{where}`" in problem
    assert config.model.light == "haiku" and config.fix.max_retries == 3


@pytest.mark.parametrize(
    ("requested", "expected"), [(1, 1), (MAX_RETRIES_CAP, MAX_RETRIES_CAP), (6, MAX_RETRIES_CAP),
                                (1000, MAX_RETRIES_CAP)],
)  # fmt: skip
def test_fix_retries_above_the_pipeline_maximum_are_clamped(requested: int, expected: int) -> None:
    """회차마다 테스트를 다시 돌리므로 리포가 큰 값을 적어도 최댓값으로 줄인다 (9.2, T23). 설정
    오류로 다른 값까지 기본값으로 되돌리지 않는다."""
    config, problem = parse_repo_config(
        f"fix: {{max_retries: {requested}, require_approval: true}}\n"
    )
    assert problem is None and config.fix.require_approval
    assert fix_limits(config.fix) == FixSettings(require_approval=True, max_retries=expected)


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
