"""검색 테스트가 같이 쓰는 것: 기록 만들기, 수정 결과 넣기, 받아 둔 임베딩 모델 열기.

임베딩 모델(multilingual-e5-small, 470MB)을 쓰는 테스트는 `PR_PROOF_REAL_EMBED=1`과
`PR_PROOF_EMBED_DIR`(`tokenizer.json`, `model.onnx`가 든 폴더)이 있을 때만 돈다.
"""

import hashlib
import os
from pathlib import Path
from typing import Final

import pytest

from app.shared.db_pure import FixAttempt
from app.shared.embed_models_pure import E5_SMALL, EMBED_MODELS, EmbedModel
from app.worker.store.memory import OnnxEncoder
from app.worker.store.records_pure import NewRecord
from app.worker.store.state import Store

REAL_EMBED_ENV: Final = "PR_PROOF_REAL_EMBED"
EMBED_DIR_ENV: Final = "PR_PROOF_EMBED_DIR"
SPEC: Final[EmbedModel] = EMBED_MODELS[E5_SMALL]
PRINT_RULE: Final = "`orders/` 아래 모듈은 `print`를 쓰지 않는다."


def embed_encoder() -> OnnxEncoder:
    """받아 둔 임베딩 모델. 없으면 테스트를 건너뛴다. 파일의 해시를 고정한 판과 맞춰 본다."""
    folder = os.environ.get(EMBED_DIR_ENV)
    if os.environ.get(REAL_EMBED_ENV) != "1" or not folder:
        pytest.skip(f"임베딩 모델을 쓰는 테스트 ({REAL_EMBED_ENV}=1, {EMBED_DIR_ENV})")
    for f in (SPEC.tokenizer, SPEC.onnx):
        data = (Path(folder) / Path(f.path).name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == f.sha256, f.path
    return OnnxEncoder(SPEC, Path(folder))


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
