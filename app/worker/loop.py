"""공용 서브그래프 틀: 작성 → 프로그램의 확인 → 이유를 넘겨 재시도 (docs/DESIGN.md 5.3).

검사기 작성과 모델 수정이 같이 쓴다. 쓰는 곳마다 다른 것은 `write`(프롬프트 + 모델 호출)와
`verify`(검사기 작성은 예시 확인, 모델 수정은 출구 검사)뿐이고, 회차·모델 단계·호출 상한은
`route`가 `LoopPolicy`로 정한다. 모델 수정은 되풀이 대부분이 `write`의 호출 하나 안에서 모델과
파이프라인 도구 사이에 일어나고(4.7), 회차는 출구 검사에 떨어졌을 때의 뒷받침이다.

    route ─(가벼운/무거운 모델)→ write ─(결과 있음)→ verify ─(통과)→ accepted
      │                            │                   ├─(재시도: feedback)→ route
      │                            └─(작성 실패)→ route └─(버림)→ dropped
      └─(회차 소진)→ exhausted · (호출 상한)→ budget

상태에는 데이터만 넣는다(5.5). `write`의 결과물(검사기 본문, 패치)은 쓰는 쪽이 캐시 테이블에
두고 그 키만 `artifact`에 넣는다. `write`와 `verify`가 따로 노드라서, 체크포인터가 있는 그래프
안에서는 `verify`부터 이어 갈 수 있다.
"""

import logging
import operator
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Final, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.worker.call_budget_pure import Tier

log = logging.getLogger(__name__)

Outcome = Literal["accepted", "exhausted", "budget", "dropped"]
AttemptResult = Literal["accepted", "rejected", "dropped", "write_failed"]

HEAVY_AFTER_FAILURES: Final = 2  # 가벼운 모델이 이만큼 실패하면 무거운 모델로 한 번 (9.3)
ROUTE: Final = "route"
WRITE: Final = "write"
VERIFY: Final = "verify"


class Attempt(TypedDict):
    """한 회차의 기록. 체크포인트에 그대로 들어가도록 dict다."""

    attempt: int  # 1부터
    tier: Tier
    result: AttemptResult
    artifact: str  # write가 낸 결과물의 키. 작성에 실패했으면 ""
    detail: str  # 실패 이유 한 줄. 통과했으면 ""
    feedback: list[str]  # 다음 회차의 write에 넘긴 것


class LoopState(TypedDict, total=False):
    task: dict[str, Any]  # 쓰는 쪽의 입력(규칙과 예시, 대상 위반). 틀은 읽지 않는다
    attempt: int  # 지금까지 시작한 회차 수
    tier: Tier  # 이번 회차의 모델 단계
    feedback: list[str]  # 직전 회차의 실패 내용. 첫 회차는 비어 있다
    artifact: str  # 이번 회차 write 결과물의 키
    history: Annotated[list[Attempt], operator.add]
    outcome: Outcome  # 끝났을 때만 있다
    detail: str  # 마지막 실패 이유 또는 버린 이유


@dataclass(frozen=True)
class WriteResult:
    """write의 결과. 본문은 쓰는 쪽이 캐시에 저장하고 여기에는 그 키만 넣는다."""

    artifact: str = ""
    error: str = ""  # 모델 호출이 실패했거나 답을 읽을 수 없으면 그 이유


@dataclass(frozen=True)
class Verdict:
    """verify의 결과. retry는 feedback을 다음 회차에 넘기고, drop은 재시도 없이 버린다."""

    status: Literal["pass", "retry", "drop"]
    detail: str = ""
    feedback: tuple[str, ...] = ()


class WriteFailed(Exception):
    """write가 올리면 그 회차를 실패로 센다. 다른 예외(환경 오류, 버그)는 그대로 올라간다."""


@dataclass(frozen=True)
class LoopPolicy:
    max_attempts: int
    # 이 단계의 모델 호출을 한 번 더 해도 되는지. 호출 상한(9.2)을 가진 쪽이 준다
    may_call: Callable[[Tier], bool]
    heavy_after: int | None = HEAVY_AFTER_FAILURES  # None이면 무거운 모델로 올리지 않는다

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError(f"max_attempts는 1 이상이어야 한다: {self.max_attempts}")
        if self.heavy_after is not None and self.heavy_after < 1:
            raise ValueError(f"heavy_after는 1 이상이어야 한다: {self.heavy_after}")


def choose_tier(history: list[Attempt], policy: LoopPolicy) -> Tier | None:
    """다음 회차의 모델 단계. 호출 상한에 걸리면 None.

    가벼운 모델로 시작하고, 가벼운 모델이 `heavy_after`회 실패하면 무거운 모델로 한 번 올린다.
    무거운 모델을 부를 여유가 없으면 올리지 않고 가벼운 모델로 계속한다.
    """
    light_failures = sum(1 for a in history if a["tier"] == "light")
    heavy_used = any(a["tier"] == "heavy" for a in history)
    wants_heavy = (
        policy.heavy_after is not None and light_failures >= policy.heavy_after and not heavy_used
    )
    if wants_heavy and policy.may_call("heavy"):
        return "heavy"
    return "light" if policy.may_call("light") else None


def build_loop(
    name: str,
    write: Callable[[LoopState], WriteResult],
    verify: Callable[[LoopState], Verdict],
    policy: LoopPolicy,
) -> CompiledStateGraph:
    """`{"task": ...}`로 시작해 `outcome`이 정해질 때까지 도는 서브그래프."""

    def record(
        state: LoopState, result: AttemptResult, detail: str, feedback: list[str]
    ) -> Attempt:
        return Attempt(
            attempt=state["attempt"], tier=state["tier"], result=result,
            artifact=state.get("artifact", ""), detail=detail, feedback=feedback,
        )  # fmt: skip

    def route_node(state: LoopState) -> LoopState:
        attempt = state.get("attempt", 0)
        if attempt >= policy.max_attempts:
            return {"outcome": "exhausted"}
        tier = choose_tier(state.get("history", []), policy)
        if tier is None:
            log.info("%s: 호출 상한으로 %d회차를 시작하지 않음", name, attempt + 1)
            return {"outcome": "budget"}
        return {"attempt": attempt + 1, "tier": tier, "artifact": ""}

    def write_node(state: LoopState) -> LoopState:
        try:
            result = write(state)
        except WriteFailed as e:
            error = str(e) or "작성에 실패함"
        else:
            if not isinstance(result, WriteResult):
                error = f"write가 WriteResult가 아닌 값을 냄: {type(result).__name__}"
            elif result.error:
                error = result.error
            elif not result.artifact:
                error = "write가 결과물 없이 끝남"
            else:
                return {"artifact": result.artifact}
        log.info("%s: %d회차 작성 실패: %s", name, state["attempt"], error)
        return {
            "feedback": [error],
            "detail": error,
            "history": [record(state, "write_failed", error, [error])],
        }

    def verify_node(state: LoopState) -> LoopState:
        verdict = verify(state)
        if not isinstance(verdict, Verdict):
            raise TypeError(f"{name}: verify가 Verdict가 아닌 값을 냄: {type(verdict).__name__}")
        if verdict.status == "pass":
            return {
                "outcome": "accepted",
                "detail": "",
                "history": [record(state, "accepted", "", [])],
            }
        if verdict.status == "drop":
            return {
                "outcome": "dropped",
                "detail": verdict.detail,
                "history": [record(state, "dropped", verdict.detail, [])],
            }
        feedback = list(verdict.feedback) or [verdict.detail]
        return {
            "feedback": feedback,
            "detail": verdict.detail,
            "history": [record(state, "rejected", verdict.detail, feedback)],
        }

    def after_route(state: LoopState) -> str:
        return END if "outcome" in state else WRITE

    def after_write(state: LoopState) -> str:
        return VERIFY if state.get("artifact") else ROUTE

    def after_verify(state: LoopState) -> str:
        return END if "outcome" in state else ROUTE

    graph = StateGraph(LoopState)
    graph.add_node(ROUTE, route_node)
    graph.add_node(WRITE, write_node)
    graph.add_node(VERIFY, verify_node)
    graph.add_edge(START, ROUTE)
    graph.add_conditional_edges(ROUTE, after_route, [WRITE, END])
    graph.add_conditional_edges(WRITE, after_write, [VERIFY, ROUTE])
    graph.add_conditional_edges(VERIFY, after_verify, [ROUTE, END])
    return graph.compile(name=name)
