"""모델 수정의 상태가 그래프의 체크포인트를 거쳐도 남는지 (docs/DESIGN.md 4.7, 4.8)."""

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.worker import graph
from app.worker.fix import model_fix_pure


def test_model_fix_state_survives_checkpoint() -> None:
    fix = model_fix_pure.ModelFix(
        status="ready", targets=("v1",),
        explanation=model_fix_pure.Explanation(
            "s", (model_fix_pure.Change("a.py", 2, "R", "w"),)
        ),
        verification=model_fix_pure.Verification(9, 1, 1, False, 1, 2), attempt_id=3,
    )  # fmt: skip
    serde = JsonPlusSerializer(allowed_msgpack_modules=list(graph._STATE_TYPES))
    restored = serde.loads_typed(serde.dumps_typed({"f": fix}))["f"]
    assert isinstance(restored.explanation.changes[0], model_fix_pure.Change)
    assert isinstance(restored.verification, model_fix_pure.Verification)
