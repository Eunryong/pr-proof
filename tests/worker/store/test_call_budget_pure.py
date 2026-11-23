from typing import Final

import pytest

from app.worker.config.repo_config_pure import CallSettings
from app.worker.store.call_budget_pure import (
    Allowed,
    CallLimits,
    Denied,
    PrScope,
    RulesetScope,
    Spent,
    decide,
    limits_for,
)

DEFAULTS: Final = CallSettings()


@pytest.mark.parametrize(
    ("rule_count", "expected"),
    [(0, 10), (1, 14), (5, 30), (22, 98), (23, 100), (100, 100)],
)
def test_ruleset_cap_grows_with_rules_up_to_max(rule_count: int, expected: int) -> None:
    limits = limits_for(DEFAULTS, RulesetScope("k", rule_count))
    assert (limits.scope_cap, limits.scope, limits.repo_daily) == ("ruleset", expected, 500)


@pytest.mark.parametrize(
    ("targets", "expected"),
    [(0, 20), (1, 24), (32, 148), (33, 150), (34, 150), (1000, 150)],
)
def test_pr_cap_grows_with_target_violations_up_to_max(targets: int, expected: int) -> None:
    """9.2 (4-5): PR 상한은 pr_base + 대상 위반 수 × per_violation이고 pr_max에서 멈춘다."""
    assert limits_for(DEFAULTS, PrScope(), targets=targets) == CallLimits("pr", expected, 500)
    assert limits_for(DEFAULTS, RulesetScope("k", 0), targets=targets).scope == 10


def test_low_pr_max_caps_even_without_violations() -> None:
    assert limits_for(CallSettings(pr_max=3), PrScope()).scope == 3
    assert limits_for(CallSettings(pr_base=0, per_violation=0), PrScope(), targets=50).scope == 0


@pytest.mark.parametrize(
    ("grants", "targets", "expected"), [(0, 0, 0), (0, 2, 2), (1, 0, 3), (1, 2, 5), (2, 9, 9)]
)
def test_each_fix_command_grants_pr_max_once_more(grants: int, targets: int, expected: int) -> None:
    """9.2: `/fix`는 PR 상한을 한 번 더(pr_max만큼) 받는다. 저장소 하루 상한과 규칙 묶음 상한은
    그대로다."""
    settings = CallSettings(pr_base=0, per_violation=1, pr_max=3)
    assert limits_for(settings, PrScope(), grants, targets) == CallLimits("pr", expected, 500)
    assert limits_for(settings, RulesetScope("k", 0), grants).scope == 10


def test_negative_rule_count_grants_or_targets_are_rejected() -> None:
    with pytest.raises(ValueError):
        limits_for(DEFAULTS, RulesetScope("k", -1))
    with pytest.raises(ValueError):
        limits_for(DEFAULTS, PrScope(), -1)
    with pytest.raises(ValueError):
        limits_for(DEFAULTS, PrScope(), targets=-1)


PR: Final = CallLimits("pr", 10, 100)


@pytest.mark.parametrize(
    ("spent", "tier", "expected"),
    [
        (Spent(0, 0), "light", Allowed(1)),
        (Spent(9, 9), "light", Allowed(1)),  # 쓴 뒤의 합이 상한과 같으면 허용
        (Spent(10, 10), "light", Denied("pr", 10, 10, 1)),
        (Spent(7, 7), "heavy", Allowed(3)),  # 무거운 모델은 3으로 센다
        (Spent(10, 99), "light", Denied("pr", 10, 10, 1)),
        (Spent(0, 99), "light", Allowed(1)),
        (Spent(0, 100), "light", Denied("repo_daily", 100, 100, 1)),
        (Spent(10, 100), "light", Denied("repo_daily", 100, 100, 1)),  # 하루 상한을 먼저 알린다
        (Spent(0, 98), "heavy", Denied("repo_daily", 98, 100, 3)),
    ],
)
def test_decide_at_cap_boundaries(spent: Spent, tier: str, expected: Allowed | Denied) -> None:
    assert decide(PR, spent, tier) == expected


@pytest.mark.parametrize(
    ("used", "heavy_allowed"),
    [(0, True), (79, True), (80, False), (96, False)],
)
def test_no_escalation_to_heavy_past_80_percent(used: int, heavy_allowed: bool) -> None:
    limits = CallLimits("ruleset", 100, 1000)
    heavy = decide(limits, Spent(used, used), "heavy")
    assert isinstance(heavy, Allowed) is heavy_allowed
    if not heavy_allowed:
        assert heavy.escalation and heavy.cap == "ruleset"
    assert decide(limits, Spent(used, used), "light") == Allowed(1)  # 가벼운 모델은 상한까지


def test_zero_cap_denies_everything() -> None:
    assert isinstance(decide(CallLimits("pr", 0, 500), Spent(0, 0), "light"), Denied)


def test_denied_reason_names_the_cap() -> None:
    assert "저장소 하루 상한" in Denied("repo_daily", 500, 500, 1).reason
    assert "규칙 묶음 상한" in Denied("ruleset", 10, 10, 1).reason
    assert "무거운 모델" in Denied("pr", 120, 150, 3, escalation=True).reason
