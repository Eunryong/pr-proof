"""실제 임베딩 모델로 재는 검색 품질 (docs/DESIGN.md 6.7, 13절 5단계의 기준값 정하기). 느린
테스트다.

`PR_PROOF_REAL_EMBED=1`일 때만 돈다. `PR_PROOF_EMBED_DIR`에 고정한 판의 multilingual-e5-small 파일
(`onnx/tokenizer.json`, `onnx/model.onnx`)이 든 폴더를 주면 그것을 쓰고(해시를 맞춰 본다), 없으면
`memory.ensure_model`로 임시 폴더에 받는다(470MB). 모델 호출은 없다.

eval/rag_recall.yaml의 기록을 임시 Chroma에 넣고 질의마다 top-k를 검색해 recall@1·3·5, 관련
있는 짝과 없는 짝의 유사도 분포를 접두어가 있을 때와 없을 때로 낸다. `PR_PROOF_RAG_OUT`이 있으면
JSON으로 쓴다.
"""

import hashlib
import json
import os
import statistics
import tempfile
from pathlib import Path
from typing import Final

import fakeredis
import pytest
import yaml

from app.models.rag import E5_SMALL, EMBED_MODELS, EmbedModel, record_text
from app.models.records import NewRecord
from app.models.repo_config import DEFAULT_MIN_SIMILARITY, RagSettings
from app.worker import memory
from app.worker.state import Store

SPEC_FILE: Final = Path(__file__).resolve().parents[2] / "eval" / "rag_recall.yaml"
REAL_EMBED_ENV: Final = "PR_PROOF_REAL_EMBED"
EMBED_DIR_ENV: Final = "PR_PROOF_EMBED_DIR"
OUT_ENV: Final = "PR_PROOF_RAG_OUT"
KS: Final = (1, 3, 5)
THRESHOLDS: Final = (0.80, 0.85, 0.88, 0.90, 0.91, 0.92, 0.93)

pytestmark = pytest.mark.skipif(
    os.environ.get(REAL_EMBED_ENV) != "1", reason="PR_PROOF_REAL_EMBED=1일 때만 (모델 470MB)"
)


def model_folder(spec: EmbedModel) -> Path:
    given = os.environ.get(EMBED_DIR_ENV)
    if not given:
        return memory.ensure_model(Path(tempfile.mkdtemp()) / "models", spec)
    folder = Path(given)
    for f in (spec.tokenizer, spec.onnx):
        assert hashlib.sha256((folder / Path(f.path).name).read_bytes()).hexdigest() == f.sha256
    return folder


class _NoPrefix:
    """접두어를 빼고 재기 위한 감싼 모델 (6.6의 2번, 11.3의 접두어 스파이크)."""

    def __init__(self, inner: memory.OnnxEncoder) -> None:
        self.inner = inner
        self.spec = EmbedModel(**{**inner.spec.__dict__, "query_prefix": "", "passage_prefix": ""})

    def encode(self, texts):
        return self.inner.encode(texts)


def measure(encoder, spec: dict, tmp: Path) -> dict:
    import numpy as np

    store = Store(tmp / "db.sqlite", fakeredis.FakeRedis())
    ids: dict[str, int] = {}
    for r in spec["records"]:
        store.add_record(NewRecord(
            repo="o/r", kind=r["kind"], origin="eval", source_id=r["id"], rule_id=r["rule"],
            rule_text=spec["rules"][r["rule"]], file="", line_text="", context=r["context"],
            diff="",
        ))  # fmt: skip
        ids[r["id"]] = store.records("o/r")[-1].id
    vectors = memory.VectorStore(tmp / "chroma", encoder.spec.name, encoder.spec.dim)
    memory.index_pending(store, vectors, encoder, "o/r")
    by_id = {v: k for k, v in ids.items()}
    recall = {k: [0, 0] for k in KS}
    related, unrelated, ranks = [], [], []
    texts = [
        record_text(r["kind"], spec["rules"][r["rule"]], r["context"]) for r in spec["records"]
    ]
    passages = memory.embed(encoder, texts, role="passage").vectors
    for q in spec["queries"]:
        text = record_text("adopted", spec["rules"][q["rule"]], q["context"])
        vector = memory.embed(encoder, [text], role="query").vectors[0]
        hits = vectors.search("o/r", vector, max(KS))
        got = [by_id[h.record_id] for h in hits]
        for k in KS:
            recall[k][0] += len(set(got[:k]) & set(q["expect"]))
            recall[k][1] += min(k, len(q["expect"]))
        sims = passages @ vector
        for r, s in zip(spec["records"], sims, strict=True):
            (related if r["id"] in q["expect"] else unrelated).append(float(s))
        top = [[g, round(h.similarity, 3)] for g, h in zip(got, hits, strict=True)]
        ranks.append({"rule": q["rule"], "top": top})
    vectors.close()

    def dist(xs: list[float]) -> dict:
        xs = sorted(xs)
        return {"n": len(xs), "min": round(xs[0], 3), "median": round(statistics.median(xs), 3),
                "max": round(xs[-1], 3)}  # fmt: skip

    by_threshold = {}
    for t in THRESHOLDS:
        by_threshold[str(t)] = {
            "related_kept": f"{sum(s >= t for s in related)}/{len(related)}",
            "unrelated_kept": f"{sum(s >= t for s in unrelated)}/{len(unrelated)}",
        }
    del np
    return {
        "recall": {f"@{k}": f"{a}/{b}" for k, (a, b) in recall.items()},
        "related": dist(related), "unrelated": dist(unrelated),
        "by_threshold": by_threshold, "ranks": ranks,
    }  # fmt: skip


def test_recall_and_similarity_distributions(tmp_path: Path) -> None:
    pytest.importorskip("chromadb")
    spec = yaml.safe_load(SPEC_FILE.read_text())
    model = EMBED_MODELS[E5_SMALL]
    encoder = memory.OnnxEncoder(model, model_folder(model))
    out = {}
    for name, enc in (("prefix", encoder), ("no_prefix", _NoPrefix(encoder))):
        folder = tmp_path / name
        folder.mkdir()
        out[name] = measure(enc, spec, folder)
    print(json.dumps({k: {x: v[x] for x in ("recall", "related", "unrelated", "by_threshold")}
                      for k, v in out.items()}, ensure_ascii=False, indent=1))  # fmt: skip
    if os.environ.get(OUT_ENV):
        target = Path(os.environ[OUT_ENV])
        target.mkdir(parents=True, exist_ok=True)
        (target / "rag_recall.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    # 관련 있는 짝은 기본 기준값을 넘고 관련 없는 짝은 대부분 못 넘어야 기준값이 뜻이 있다
    kept = out["prefix"]["by_threshold"][str(DEFAULT_MIN_SIMILARITY)]
    assert kept["related_kept"].split("/")[0] != "0", out["prefix"]
    assert RagSettings().min_similarity == DEFAULT_MIN_SIMILARITY
