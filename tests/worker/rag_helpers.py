"""검색 테스트의 가짜 임베딩 모델. 실제 모델(470MB)을 받지 않고 결정적으로 벡터를 만든다.

글자 3-gram을 해시해 센 벡터라 같은 낱말이 많은 글일수록 가깝다. 정규화하지 않은 값을 돌려줘
`memory.embed`가 정규화하는지도 본다. 받은 글(접두어 포함)을 `seen`에 남긴다.
"""

import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from app.shared.db_pure import FixAttempt
from app.shared.embed_models_pure import EmbedModel, ModelFile
from app.worker.memory import Retriever, VectorStore
from app.worker.records_pure import NewRecord
from app.worker.repo_config_pure import RagSettings
from app.worker.state import Store

FAKE_DIM: Final = 256
PRINT_RULE: Final = "`orders/` 아래 모듈은 `print`를 쓰지 않는다."
FAKE_SPEC: Final = EmbedModel(
    name="test/fake-embed",
    revision="0" * 40,
    dim=FAKE_DIM,
    query_prefix="query: ",
    passage_prefix="passage: ",
    tokenizer=ModelFile("tokenizer.json", "0" * 64, 1),
    onnx=ModelFile("model.onnx", "0" * 64, 1),
)


def record(
    kind: str, n: int, *, repo: str = "o/r", rule_text: str = PRINT_RULE,
    context: str = "x = 1\nprint(x)", diff: str = "-print(x)\n+log.info(x)", rule_id: str = "R-1",
) -> NewRecord:  # fmt: skip
    return NewRecord(
        repo=repo, kind=kind, origin="fix_pr", source_id=f"{kind}:{n}", rule_id=rule_id,
        rule_text=rule_text, file=f"orders/m{n}.py", line_text="print(x)", context=context,
        diff=diff, pr_number=3, fix_pr_number=10 + n,
    )  # fmt: skip


def fix_outcome(
    store: Store, pr: int, state: str, references: str | None, *, bundle: str = "model",
    repo: str = "o/r",
) -> int:  # fmt: skip
    """수정 시도 하나와 그것을 올린 수정안 PR (state: open, merged, rejected, superseded 등).
    PR마다 수정안이 하나라 pr을 다르게 준다. 수정 시도의 id."""
    attempt_id = store.save_fix_attempt(FixAttempt(
        repo=repo, pr_number=pr, head_sha="h", merge_base="m", bundle=bundle, status="ready",
        attempts=1, models_json="[]", targets_json="[]", references_json=references,
    ))  # fmt: skip
    store.record_proposal(repo, pr, bundle, "h", f"pipeline/pr-{pr}", 1000 + pr, attempt_id)
    if state != "open":
        (row,) = store.proposals(repo, pr)
        store.set_proposal_state(row.id, state)
    return attempt_id


def fix_outcomes(
    store: Store, start: int, count: int, *, adopted: int, references: str | None,
    repo: str = "o/r",
) -> int:  # fmt: skip
    """결과를 아는 모델 수정 시도 count건 중 adopted건은 머지, 나머지는 거절. 다음 pr 번호."""
    for i in range(count):
        state = "merged" if i < adopted else "rejected"
        fix_outcome(store, start + i, state, references, repo=repo)
    return start + count


def seeded(
    store: Store, path: Path, records: Sequence[NewRecord], settings: RagSettings,
    repo: str = "o/r",
) -> Retriever:  # fmt: skip
    """records를 저장한 Store와 그것을 찾는 진짜 Retriever(임시 Chroma, 가짜 모델)."""
    for r in records:
        store.add_record(r)
    vectors = VectorStore(path / "chroma", FAKE_SPEC.name, FAKE_SPEC.dim)
    return Retriever(store, repo, settings, vectors, FakeEncoder())


class FakeEncoder:
    def __init__(self, spec: EmbedModel = FAKE_SPEC) -> None:
        self.spec = spec
        self.seen: list[str] = []

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        self.seen += list(texts)
        out = []
        for text in texts:
            body = text.split(": ", 1)[1] if ": " in text[:10] else text
            vector = [0.0] * self.spec.dim
            for i in range(max(1, len(body) - 2)):
                gram = body[i : i + 3]
                slot = int(hashlib.md5(gram.encode()).hexdigest(), 16) % self.spec.dim
                vector[slot] += 1.0
            vector[0] += 0.5  # 빈 글도 0 벡터가 되지 않게
            out.append(vector)
        return out
