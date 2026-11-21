"""worker/loop.py: 가짜 write·verify로 틀의 분기만 확인한다 (모델·Docker 없음)."""

import json
import sqlite3
from collections.abc import Callable
from typing import Any, Final

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from worker.loop import (
    LoopPolicy,
    LoopState,
    Tier,
    Verdict,
    WriteFailed,
    WriteResult,
    build_loop,
    choose_tier,
)

PASS: Final = Verdict("pass")


def always(_tier: Tier) -> bool:
    return True


class Fakes:
    """회차마다 정해 둔 결과를 내고, 받은 상태를 적어 둔다."""

    def __init__(self, writes: list[Any], verdicts: list[Any]) -> None:
        self.writes = list(writes)
        self.verdicts = list(verdicts)
        self.write_seen: list[LoopState] = []
        self.verify_seen: list[LoopState] = []

    def write(self, state: LoopState) -> WriteResult:
        self.write_seen.append(dict(state))  # type: ignore[arg-type]
        result = self.writes.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def verify(self, state: LoopState) -> Verdict:
        self.verify_seen.append(dict(state))  # type: ignore[arg-type]
        return self.verdicts.pop(0)


def run(
    fakes: Fakes,
    *,
    max_attempts: int = 3,
    may_call: Callable[[Tier], bool] = always,
    heavy_after: int | None = 2,
) -> LoopState:
    policy = LoopPolicy(max_attempts=max_attempts, may_call=may_call, heavy_after=heavy_after)
    loop = build_loop("test", fakes.write, fakes.verify, policy)
    return loop.invoke({"task": {"rule": "R-1"}})


def artifacts(n: int) -> list[WriteResult]:
    return [WriteResult(artifact=f"key-{i}") for i in range(1, n + 1)]


def retry(detail: str, *feedback: str) -> Verdict:
    return Verdict("retry", detail=detail, feedback=feedback)


def test_accepted_on_first_attempt() -> None:
    fakes = Fakes(artifacts(1), [PASS])
    state = run(fakes)
    assert state["outcome"] == "accepted"
    assert state["attempt"] == 1
    assert state["artifact"] == "key-1"
    assert state["detail"] == ""
    assert state["history"] == [
        {"attempt": 1, "tier": "light", "result": "accepted", "artifact": "key-1",
         "detail": "", "feedback": []}
    ]  # fmt: skip
    # 첫 회차의 write는 입력과 빈 feedback을, verify는 그 회차의 결과물 키를 본다
    assert fakes.write_seen[0]["task"] == {"rule": "R-1"}
    assert fakes.write_seen[0].get("feedback", []) == []
    assert fakes.verify_seen[0]["artifact"] == "key-1"


def test_retry_passes_previous_failure_to_write() -> None:
    fakes = Fakes(
        artifacts(3),
        [
            retry("예시 2개를 틀리게 가림", "bad_1: 잡지 못함", "good_2: 잡음"),
            retry("엔진 오류"),
            PASS,
        ],
    )
    state = run(fakes)
    assert state["outcome"] == "accepted"
    assert state["attempt"] == 3
    assert state["artifact"] == "key-3"
    assert fakes.write_seen[1]["feedback"] == ["bad_1: 잡지 못함", "good_2: 잡음"]
    assert fakes.write_seen[2]["feedback"] == ["엔진 오류"]  # feedback이 없으면 detail을 넘긴다
    assert [a["result"] for a in state["history"]] == ["rejected", "rejected", "accepted"]
    assert [a["artifact"] for a in state["history"]] == ["key-1", "key-2", "key-3"]
    assert state["history"][0]["feedback"] == ["bad_1: 잡지 못함", "good_2: 잡음"]
    assert state["detail"] == ""  # 통과하면 이전 회차의 실패 이유를 남기지 않는다


@pytest.mark.parametrize("max_attempts", [1, 3, 5])
def test_gives_up_at_max_attempts(max_attempts: int) -> None:
    fakes = Fakes(artifacts(max_attempts), [retry(f"실패 {i}") for i in range(1, max_attempts + 1)])
    state = run(fakes, max_attempts=max_attempts)
    assert state["outcome"] == "exhausted"
    assert state["attempt"] == max_attempts
    assert len(fakes.write_seen) == max_attempts  # 상한을 넘겨 부르지 않는다
    assert len(state["history"]) == max_attempts
    assert state["detail"] == f"실패 {max_attempts}"  # 마지막 실패 이유


def test_identical_failure_does_not_stop_early() -> None:
    """설계에 같은 실패로 일찍 멈추는 규칙이 없다. 같은 실패가 이어져야 무거운 모델로 올라간다."""
    fakes = Fakes(artifacts(3), [retry("같은 실패")] * 3)
    state = run(fakes)
    assert state["outcome"] == "exhausted"
    assert [a["tier"] for a in state["history"]] == ["light", "light", "heavy"]


def test_drop_ends_without_retry() -> None:
    fakes = Fakes(artifacts(1), [Verdict("drop", detail="허용 범위 밖 파일을 바꿈")])
    state = run(fakes)
    assert state["outcome"] == "dropped"
    assert state["detail"] == "허용 범위 밖 파일을 바꿈"
    assert len(fakes.write_seen) == 1
    assert state["history"][0]["result"] == "dropped"


def test_budget_denied_before_first_call() -> None:
    fakes = Fakes([], [])
    state = run(fakes, may_call=lambda tier: False)
    assert state["outcome"] == "budget"
    assert state.get("attempt", 0) == 0
    assert state.get("history", []) == []
    assert fakes.write_seen == []


def test_budget_denied_between_attempts() -> None:
    allowed = iter([True, False])
    fakes = Fakes(artifacts(1), [retry("실패 1")])
    state = run(fakes, may_call=lambda tier: next(allowed))
    assert state["outcome"] == "budget"  # 회차 소진(exhausted)과 구분된다
    assert state["attempt"] == 1
    assert state["detail"] == "실패 1"
    assert len(fakes.write_seen) == 1


def test_budget_is_not_asked_when_attempts_are_exhausted() -> None:
    asked: list[Tier] = []

    def may_call(tier: Tier) -> bool:
        asked.append(tier)
        return True

    state = run(Fakes(artifacts(1), [retry("실패")]), max_attempts=1, may_call=may_call)
    assert state["outcome"] == "exhausted"
    assert asked == ["light"]


def test_write_failure_counts_as_attempt_and_skips_verify() -> None:
    fakes = Fakes(
        [WriteResult(error="시간 초과"), WriteFailed("답이 JSON이 아님"), WriteResult("key-3")],
        [PASS],
    )
    state = run(fakes)
    assert state["outcome"] == "accepted"
    assert state["attempt"] == 3
    assert len(fakes.verify_seen) == 1  # 결과물이 없는 회차는 검증하지 않는다
    assert [a["result"] for a in state["history"]] == ["write_failed", "write_failed", "accepted"]
    assert [a["artifact"] for a in state["history"]] == ["", "", "key-3"]
    assert fakes.write_seen[1]["feedback"] == ["시간 초과"]
    assert fakes.write_seen[2]["feedback"] == ["답이 JSON이 아님"]


@pytest.mark.parametrize(
    ("result", "detail"),
    [
        (WriteResult(), "write가 결과물 없이 끝남"),
        (None, "write가 WriteResult가 아닌 값을 냄: NoneType"),
        ("key", "write가 WriteResult가 아닌 값을 냄: str"),
        (WriteFailed(), "작성에 실패함"),
    ],
)
def test_invalid_write_output_is_a_failed_attempt(result: Any, detail: str) -> None:
    fakes = Fakes([result], [])
    state = run(fakes, max_attempts=1)
    assert state["outcome"] == "exhausted"
    assert state["detail"] == detail
    assert state["history"][0]["result"] == "write_failed"
    assert fakes.verify_seen == []


def test_failed_write_does_not_reuse_previous_artifact() -> None:
    fakes = Fakes([WriteResult("key-1"), WriteResult(error="호출 실패")], [retry("실패 1")])
    state = run(fakes, max_attempts=2)
    assert state["outcome"] == "exhausted"
    assert state["artifact"] == ""
    assert len(fakes.verify_seen) == 1  # 앞 회차의 결과물을 다시 검증하지 않는다


def test_other_exceptions_propagate() -> None:
    """환경 오류와 버그는 회차로 세지 않고 올린다 (RQ가 재시도한다)."""
    with pytest.raises(RuntimeError, match="claude 로그인이 풀림"):
        run(Fakes([RuntimeError("claude 로그인이 풀림")], []))
    with pytest.raises(TypeError, match="Verdict가 아닌 값"):
        run(Fakes(artifacts(1), [True]))


def test_heavy_once_after_two_light_failures() -> None:
    fakes = Fakes(artifacts(5), [retry(f"실패 {i}") for i in range(5)])
    state = run(fakes, max_attempts=5)
    assert [a["tier"] for a in state["history"]] == ["light", "light", "heavy", "light", "light"]
    assert [s["tier"] for s in fakes.write_seen] == ["light", "light", "heavy", "light", "light"]


def test_write_failures_count_toward_heavy() -> None:
    fakes = Fakes([WriteResult(error="실패")] * 2 + [WriteResult("key-3")], [PASS])
    state = run(fakes)
    assert [a["tier"] for a in state["history"]] == ["light", "light", "heavy"]


def test_stays_light_when_heavy_is_not_affordable() -> None:
    fakes = Fakes(artifacts(3), [retry("실패")] * 3)
    state = run(fakes, may_call=lambda tier: tier == "light")
    assert state["outcome"] == "exhausted"
    assert [a["tier"] for a in state["history"]] == ["light", "light", "light"]


def test_no_heavy_when_disabled() -> None:
    fakes = Fakes(artifacts(3), [retry("실패")] * 3)
    state = run(fakes, heavy_after=None)
    assert [a["tier"] for a in state["history"]] == ["light", "light", "light"]


@pytest.mark.parametrize(
    ("tiers", "heavy_after", "expected"),
    [
        ([], 2, "light"),
        (["light"], 2, "light"),  # 경계 바로 아래
        (["light", "light"], 2, "heavy"),  # 경계
        (["light"], 1, "heavy"),
        (["light", "light", "heavy"], 2, "light"),  # 무거운 모델은 한 번만
        (["light", "light"], None, "light"),
    ],
)
def test_choose_tier(tiers: list[Tier], heavy_after: int | None, expected: Tier) -> None:
    history = [
        {
            "attempt": i,
            "tier": t,
            "result": "rejected",
            "artifact": "",
            "detail": "",
            "feedback": [],
        }
        for i, t in enumerate(tiers, 1)
    ]
    policy = LoopPolicy(max_attempts=5, may_call=always, heavy_after=heavy_after)
    assert choose_tier(history, policy) == expected  # type: ignore[arg-type]


def test_choose_tier_none_when_budget_denies() -> None:
    policy = LoopPolicy(max_attempts=3, may_call=lambda tier: False)
    assert choose_tier([], policy) is None


@pytest.mark.parametrize(
    "kwargs", [{"max_attempts": 0}, {"max_attempts": -1}, {"max_attempts": 3, "heavy_after": 0}]
)
def test_policy_rejects_invalid_values(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        LoopPolicy(may_call=always, **kwargs)


def test_final_state_is_plain_data() -> None:
    fakes = Fakes(artifacts(2), [retry("실패", "남은 위반 1건"), PASS])
    state = run(fakes)
    assert json.loads(json.dumps(state)) == state


def test_runs_as_subgraph_and_resumes_from_verify() -> None:
    """부모의 체크포인터를 쓴다. verify에서 죽으면 write를 다시 부르지 않고 verify부터 잇는다."""
    fakes = Fakes(artifacts(1), [PASS])
    crash = [True]

    def verify(state: LoopState) -> Verdict:
        if crash.pop() if crash else False:
            raise RuntimeError("샌드박스 실패")
        return fakes.verify(state)

    loop = build_loop("author", fakes.write, verify, LoopPolicy(max_attempts=3, may_call=always))
    parent = StateGraph(LoopState)
    parent.add_node("author", loop)
    parent.add_edge(START, "author")
    parent.add_edge("author", END)
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    graph = parent.compile(checkpointer=SqliteSaver(conn))
    config = {"configurable": {"thread_id": "t"}}

    with pytest.raises(RuntimeError, match="샌드박스 실패"):
        graph.invoke({"task": {"rule": "R-1"}}, config)
    state = graph.invoke(None, config)

    assert state["outcome"] == "accepted"
    assert len(fakes.write_seen) == 1
    assert state["history"][0]["artifact"] == "key-1"
