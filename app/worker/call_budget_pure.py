"""모델 호출 수 상한 (docs/DESIGN.md 9.2, 9.3). 판단만 한다. 세고 기록하는 것은
app/worker/budget.py.

상한은 셋이다. PR 상한(그 PR의 모든 커밋 누적), 규칙 묶음 상한(규칙 읽기와 검사기 작성, 그 묶음의
캐시 키 단위), 저장소 하루 상한(그 저장소의 모든 호출, 최근 24시간). 무거운 모델 호출은 3으로,
저장된 결과를 쓴 것은 0으로 센다.

PR 상한은 `pr_base` + 대상 위반 수 × `per_violation`이고 `pr_max`에서 멈춘다. `/fix`로 다시 돌릴
때마다 `pr_max`를 한 번 더 받는다 (9.2 "걸렸을 때"). 저장소 하루 상한은 그대로다.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Final, Literal

from app.worker.repo_config_pure import CallSettings

Tier = Literal["light", "heavy"]
# ai_calls의 단계 (7.8). 규칙 읽기, 검사기 작성, PR 패턴 작성(pr_plan)은 규칙 묶음 상한에서,
# 나머지는 PR 상한에서 센다. pr_judge는 PR 단위 확인의 모델 판단(4.5 끝), run_judge는 실행 결과의
# 규칙의 모델 판단이다 (4.5). feedback은 PR 코멘트의 판단이다 (4.11)
Stage = Literal[
    "rules", "checker", "pr_plan", "judge", "pr_judge", "run_judge", "decide", "fix", "feedback"
]
Cap = Literal["pr", "ruleset", "repo_daily"]

TIER_COST: Final[Mapping[str, int]] = {"light": 1, "heavy": 3}
# 호출 하나의 claude `--max-budget-usd` (9.2 "호출 하나"). 호출 하나가 끝없이 도는 것을 막을 뿐
# 상한 계산에는 쓰지 않는다
CALL_BUDGET_USD: Final[Mapping[str, float]] = {"light": 0.30, "heavy": 0.50}
DAILY_WINDOW: Final = timedelta(hours=24)
# 이만큼 쓴 상한에서는 무거운 모델로 올리지 않는다 (9.3). 가벼운 모델은 상한까지 쓴다
HEAVY_STOP_RATIO: Final = 0.8
CAP_LABEL: Final[Mapping[str, str]] = {
    "pr": "PR 상한",
    "ruleset": "규칙 묶음 상한",
    "repo_daily": "저장소 하루 상한",
}


@dataclass(frozen=True)
class PrScope:
    """PR 상한에서 세는 호출. 어느 PR인지는 호출을 세는 쪽(app/worker/budget.py)이 안다."""


@dataclass(frozen=True)
class RulesetScope:
    """규칙 묶음 상한에서 세는 호출. rule_count는 지금까지 읽은 규칙 수(규칙을 읽기 전이면 0)."""

    key: str
    rule_count: int = 0


Scope = PrScope | RulesetScope


@dataclass(frozen=True)
class CallLimits:
    scope_cap: Cap  # "pr" 또는 "ruleset"
    scope: int  # 그 상한의 값
    repo_daily: int


@dataclass(frozen=True)
class Spent:
    """지금까지 센 값. scope는 그 PR(전체 기간) 또는 규칙 묶음(최근 24시간)에서, repo_daily는 최근
    24시간 저장소에서."""

    scope: int
    repo_daily: int


@dataclass(frozen=True)
class Allowed:
    cost: int  # 이 호출이 상한에서 차지하는 값


@dataclass(frozen=True)
class Denied:
    cap: Cap
    used: int
    limit: int
    cost: int
    # 상한에는 여유가 있지만 80%를 넘겨 무거운 모델로 올리지 않는 경우. 가벼운 모델은 부를 수 있다
    escalation: bool = False

    @property
    def reason(self) -> str:
        label = CAP_LABEL[self.cap]
        if self.escalation:
            return f"{label}의 80%를 써서 무거운 모델로 올리지 않음 ({self.used}/{self.limit}회)"
        return f"호출 상한: {label} ({self.used}/{self.limit}회 사용, 이 호출 {self.cost}회)"


def limits_for(
    settings: CallSettings, scope: Scope, pr_grants: int = 0, targets: int = 0
) -> CallLimits:
    """pr_grants는 그 PR에 `/fix`가 PR 상한을 더 준 횟수, targets는 대상 위반 수다 (9.2). 둘 다 규칙
    묶음 상한에는 쓰지 않는다.

    `/fix`가 더 준 몫은 `pr_max`씩이다: `min(pr_base + targets × per_violation, pr_max)`에
    `pr_max × pr_grants`를 더한다.
    """
    if isinstance(scope, RulesetScope):
        if scope.rule_count < 0:
            raise ValueError(f"규칙 수가 음수다: {scope.rule_count}")
        cap = settings.ruleset_base + scope.rule_count * settings.per_rule
        return CallLimits("ruleset", min(cap, settings.ruleset_max), settings.repo_daily)
    if pr_grants < 0:
        raise ValueError(f"상한을 더 준 횟수가 음수다: {pr_grants}")
    if targets < 0:
        raise ValueError(f"대상 위반 수가 음수다: {targets}")
    cap = min(settings.pr_base + targets * settings.per_violation, settings.pr_max)
    return CallLimits("pr", cap + settings.pr_max * pr_grants, settings.repo_daily)


def decide(limits: CallLimits, spent: Spent, tier: Tier) -> Allowed | Denied:
    """이 단계의 모델을 한 번 더 불러도 되는지. 쓴 뒤의 합이 상한과 같으면 허용, 넘으면 거부.

    둘 다 걸리면 저장소 하루 상한을 알린다. 모델을 쓰는 단계를 모두 멈추는 쪽이다.
    """
    cost = TIER_COST[tier]
    if spent.repo_daily + cost > limits.repo_daily:
        return Denied("repo_daily", spent.repo_daily, limits.repo_daily, cost)
    if spent.scope + cost > limits.scope:
        return Denied(limits.scope_cap, spent.scope, limits.scope, cost)
    if tier == "heavy" and spent.scope >= limits.scope * HEAVY_STOP_RATIO:
        return Denied(limits.scope_cap, spent.scope, limits.scope, cost, escalation=True)
    return Allowed(cost)
