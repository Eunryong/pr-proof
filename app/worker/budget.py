"""모델 호출 수 상한을 `ai_calls`에 묶는다 (docs/DESIGN.md 9.2). 판단은 models/call_budget.py.

호출은 시작할 때 센 값과 함께 한 줄을 쓰고(`begin`), 끝나면 결과를 채운다(`finish`). 워커가
호출 도중 죽어도 그 호출은 상한에서 센다. 저장된 결과를 쓴 것은 0으로 한 줄 남긴다(`record_hit`).
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from app.models.call_budget import (
    DAILY_WINDOW,
    Allowed,
    Denied,
    RulesetScope,
    Scope,
    Stage,
    Tier,
    decide,
    limits_for,
)
from app.models.db import ModelCall
from app.models.ops_alert import Alert
from app.models.repo_config import CallSettings
from app.notify import slack
from app.worker.state import Store


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class CallBudget:
    """한 작업(PR의 한 커밋)이 쓰는 호출 상한. settings는 파이프라인 최댓값으로 줄인 값이다."""

    store: Store
    repo: str
    pr_number: int
    head_sha: str
    settings: CallSettings
    now: Callable[[], datetime] = _utc_now
    # 대상 위반 수 (9.2): 이 검사에서 찾은 위반 중 수정 판단의 표가 skip으로 정하지 않은 것. PR
    # 상한이 이만큼 는다. 부르는 쪽이 아는 만큼 `with_targets`로 넘긴다
    targets: int = 0

    def with_targets(self, targets: int) -> "CallBudget":
        return replace(self, targets=targets)

    def decide(self, scope: Scope, tier: Tier) -> Allowed | Denied:
        since = self.now().astimezone(UTC) - DAILY_WINDOW
        spent = self.store.calls_spent(self.repo, self.pr_number, scope, since)
        grants = self.store.call_grants(self.repo, self.pr_number)
        return decide(limits_for(self.settings, scope, grants, self.targets), spent, tier)

    def may_call(self, scope: Scope) -> Callable[[Tier], bool]:
        """worker/loop.py의 `LoopPolicy.may_call`에 넘긴다."""
        return lambda tier: isinstance(self.decide(scope, tier), Allowed)

    def begin(
        self, scope: Scope, *, stage: Stage, tier: Tier, model: str, attempt: int = 1
    ) -> int | Denied:
        """상한 안이면 호출 한 줄을 쓰고 그 id를, 아니면 Denied를 돌려준다."""
        verdict = self.decide(scope, tier)
        if isinstance(verdict, Denied):
            return verdict
        return self.store.add_model_call(
            self._row(scope, stage=stage, tier=tier, model=model, attempt=attempt,
                      counted=verdict.cost)
        )  # fmt: skip

    def finish(
        self,
        call_id: int,
        *,
        ok: bool,
        error: str | None = None,
        model_id: str = "",
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        permission_denials: int = 0,
    ) -> None:
        self.store.finish_model_call(
            call_id, ok=ok, error=error, model_id=model_id, input_tokens=input_tokens,
            output_tokens=output_tokens, permission_denials=permission_denials,
        )  # fmt: skip
        if permission_denials:
            slack.send(Alert("tool_denied", self.repo, self.pr_number), self.store.redis)

    def record_hit(self, scope: Scope, *, stage: Stage, tier: Tier, model: str) -> None:
        """저장된 결과를 썼다. 상한에서는 0으로 센다."""
        row = self._row(scope, stage=stage, tier=tier, model=model, attempt=1, counted=0)
        row.cache_hit, row.ok = True, True
        self.store.add_model_call(row)

    def _row(
        self, scope: Scope, *, stage: Stage, tier: Tier, model: str, attempt: int, counted: int
    ) -> ModelCall:
        return ModelCall(
            repo=self.repo, pr_number=self.pr_number, head_sha=self.head_sha, stage=stage,
            ruleset_key=scope.key if isinstance(scope, RulesetScope) else None, model=model,
            tier=tier, attempt=attempt, counted=counted, created_at=self.now().astimezone(UTC),
        )  # fmt: skip
