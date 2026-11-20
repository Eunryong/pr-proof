"""worker/budget.py: 호출 상한을 실제 SQLite의 ai_calls 기록으로 센다."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import fakeredis
import pytest

from app.models.call_budget import Allowed, Denied, PrScope, RulesetScope
from app.models.repo_config import CallSettings
from app.worker.budget import CallBudget
from app.worker.loop import LoopPolicy, choose_tier
from app.worker.state import Store

T0: Final = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self, at: datetime = T0) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "pipeline.db", fakeredis.FakeStrictRedis())


def _budget(store: Store, *, pr: int = 1, head: str = "h1", clock: Clock | None = None,
            **settings: int) -> CallBudget:  # fmt: skip
    return CallBudget(store, "o/r", pr, head, CallSettings(**settings), now=clock or Clock())


def _spend(budget: CallBudget, scope, n: int, tier: str = "light") -> None:
    for _ in range(n):
        call = budget.begin(scope, stage="rules", tier=tier, model="haiku")
        assert isinstance(call, int)
        budget.finish(call, ok=True)


def test_pr_cap_counts_every_commit_of_the_pr(store: Store) -> None:
    _spend(_budget(store, head="h1", pr_max=3), PrScope(), 2)
    second_commit = _budget(store, head="h2", pr_max=3)
    assert second_commit.decide(PrScope(), "light") == Allowed(1)
    _spend(second_commit, PrScope(), 1)
    assert second_commit.decide(PrScope(), "light") == Denied("pr", 3, 3, 1)
    assert _budget(store, pr=2, pr_max=3).decide(PrScope(), "light") == Allowed(1)  # 다른 PR


def test_heavy_counts_three_and_denied_call_is_not_recorded(store: Store) -> None:
    budget = _budget(store, pr_max=4)
    _spend(budget, PrScope(), 1, tier="heavy")
    denied = budget.begin(PrScope(), stage="fix", tier="heavy", model="sonnet")
    assert denied == Denied("pr", 3, 4, 3)
    assert [c.counted for c in store.model_calls("o/r", 1)] == [3]
    assert isinstance(budget.begin(PrScope(), stage="fix", tier="light", model="haiku"), int)


def test_cache_hits_count_zero(store: Store) -> None:
    budget = _budget(store, ruleset_base=1, per_rule=0)
    scope = RulesetScope("key-a")
    for _ in range(5):
        budget.record_hit(scope, stage="rules", tier="light", model="haiku")
    assert budget.decide(scope, "light") == Allowed(1)
    calls = store.model_calls("o/r", 1)
    assert [(c.counted, c.cache_hit, c.ok) for c in calls] == [(0, True, True)] * 5


def test_ruleset_cap_is_per_key_and_shared_across_prs(store: Store) -> None:
    scope = RulesetScope("key-a", rule_count=1)  # 2 + 1 × 1 = 3
    settings = {"ruleset_base": 2, "per_rule": 1}
    _spend(_budget(store, pr=1, **settings), scope, 2)
    _spend(_budget(store, pr=2, **settings), scope, 1)
    assert _budget(store, pr=3, **settings).decide(scope, "light") == Denied("ruleset", 3, 3, 1)
    assert _budget(store, pr=3, **settings).decide(RulesetScope("key-b", 1), "light") == Allowed(1)
    # 규칙 묶음 호출은 PR 상한에서 세지 않는다
    assert _budget(store, pr=1, pr_max=1).decide(PrScope(), "light") == Allowed(1)


def test_ruleset_cap_is_a_rolling_24_hours(store: Store) -> None:
    clock, scope = Clock(T0), RulesetScope("key-a", rule_count=1)
    settings = {"ruleset_base": 2, "per_rule": 1, "clock": clock}
    _spend(_budget(store, pr=1, **settings), scope, 3)
    assert _budget(store, pr=2, **settings).decide(scope, "light") == Denied("ruleset", 3, 3, 1)
    clock.at = T0 + timedelta(hours=23, minutes=59)
    assert isinstance(_budget(store, pr=2, **settings).decide(scope, "light"), Denied)
    clock.at = T0 + timedelta(hours=24, minutes=1)
    assert _budget(store, pr=2, **settings).decide(scope, "light") == Allowed(1)


def test_repo_daily_cap_is_a_rolling_24_hours(store: Store) -> None:
    clock = Clock(T0)
    _spend(_budget(store, pr=1, clock=clock, repo_daily=3), PrScope(), 3)
    assert _budget(store, pr=2, clock=clock, repo_daily=3).decide(PrScope(), "light") == Denied(
        "repo_daily", 3, 3, 1
    )
    clock.at = T0 + timedelta(hours=23, minutes=59)
    assert isinstance(_budget(store, pr=2, clock=clock, repo_daily=3).decide(PrScope(), "light"),
                      Denied)  # fmt: skip
    clock.at = T0 + timedelta(hours=24, seconds=1)
    assert _budget(store, pr=2, clock=clock, repo_daily=3).decide(PrScope(), "light") == Allowed(1)


def test_unfinished_call_still_counts(store: Store) -> None:
    budget = _budget(store, pr_max=1)
    assert isinstance(budget.begin(PrScope(), stage="fix", tier="light", model="haiku"), int)
    assert isinstance(budget.decide(PrScope(), "light"), Denied)
    assert store.model_calls("o/r", 1)[0].ok is None


def test_finish_records_outcome(store: Store) -> None:
    budget = _budget(store)
    call = budget.begin(RulesetScope("k"), stage="rules", tier="light", model="haiku")
    budget.finish(call, ok=False, error="timeout", model_id="claude-haiku-5-5", input_tokens=120,
                  output_tokens=30, permission_denials=1)  # fmt: skip
    row = store.model_calls("o/r", 1)[0]
    assert (row.ok, row.error, row.model_id, row.input_tokens, row.output_tokens) == (
        False, "timeout", "claude-haiku-5-5", 120, 30,
    )  # fmt: skip
    assert (row.ruleset_key, row.permission_denials, row.counted) == ("k", 1, 1)


def test_may_call_feeds_the_loop_policy(store: Store) -> None:
    budget = _budget(store, ruleset_base=10, per_rule=0)
    scope = RulesetScope("k")
    policy = LoopPolicy(max_attempts=3, may_call=budget.may_call(scope))
    two_light_failures = [
        {"attempt": i, "tier": "light", "result": "rejected", "artifact": "", "detail": "",
         "feedback": []}
        for i in (1, 2)
    ]  # fmt: skip
    assert choose_tier(two_light_failures, policy) == "heavy"
    _spend(budget, scope, 8)  # 80%를 넘기면 무거운 모델로 올리지 않는다
    assert choose_tier(two_light_failures, policy) == "light"
    _spend(budget, scope, 2)
    assert choose_tier(two_light_failures, policy) is None


def test_model_cache_round_trip(store: Store) -> None:
    assert store.cached_result("k") is None
    store.save_result("k", stage="checker", model="m", prompt_version="1", result_json='{"a":1}')
    store.save_result("k", stage="checker", model="m", prompt_version="1", result_json='{"a":2}')
    entry = store.cached_result("k")
    assert (entry.stage, entry.result_json) == ("checker", '{"a":2}')


def test_denied_tool_calls_alert_the_operator(store: Store, monkeypatch) -> None:
    """모델이 목록 밖 도구를 부르려 한 호출은 운영 알림으로 간다 (위협 T25). 없으면 알리지
    않는다."""
    from app.models.ops_alert import Alert
    from app.notify import slack

    sent: list[Alert] = []
    monkeypatch.setattr(slack, "send", lambda alert, redis, **kw: sent.append(alert) or "sent")
    budget = _budget(store, pr=7)
    clean = budget.begin(PrScope(), stage="fix", tier="light", model="haiku")
    budget.finish(clean, ok=True, permission_denials=0)
    assert sent == []
    denied = budget.begin(PrScope(), stage="fix", tier="light", model="haiku")
    budget.finish(denied, ok=True, permission_denials=2)
    assert sent == [Alert("tool_denied", "o/r", 7)]
