"""검색의 임베딩과 벡터 저장소 (DESIGN.md 6.6의 2·3·5번, 6.7).

Chroma는 진짜(임시 폴더의 `PersistentClient`)이고 임베딩 모델은 쓰지 않는다: 시드를 고정한 가짜
벡터와 글자 3-gram 가짜 모델로 한다. chromadb가 없는 설치(선택 의존성 `[rag]`를 깔지 않음)에서는
건너뛴다. 실제 모델은 `PR_PROOF_REAL_EMBED=1`의 느린 테스트(`test_real_embed.py`)가 본다.
"""

import hashlib
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest

from app.shared.embed_models_pure import ModelFile
from app.worker import memory
from app.worker.config.repo_config_pure import RagSettings
from app.worker.rag_pure import query_text
from app.worker.records_pure import NewRecord
from app.worker.state import Store

from ..redis_stand_ins import MemoryKeys
from .rag_helpers import FAKE_SPEC, FakeEncoder

np = pytest.importorskip("numpy")
pytest.importorskip("chromadb")

REPO: Final = "o/r"


def _store(tmp_path: Path) -> Store:
    return Store(tmp_path / "db.sqlite", MemoryKeys())


def _vectors(n: int, dim: int, seed: int = 7):
    rng = np.random.default_rng(seed)
    v = rng.normal(size=(n, dim)).astype(np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def _reference(matrix, q, k: int) -> list[tuple[int, float]]:
    sims = matrix @ q
    order = np.argsort(-sims, kind="stable")[:k]
    return [(int(i), float(sims[i])) for i in order]


def _same_ranking(got: list[tuple[int, float]], want: list[tuple[int, float]]) -> None:
    """같은 id가 같은 순서로 나오고(유사도 차이 1e-6 이하의 동점은 순서를 보지 않음), 유사도 차이가
    1e-4 이하다 (6.7)."""
    assert len(got) == len(want)
    want_sim = dict(want)
    for (gid, gs), (wid, ws) in zip(got, want, strict=True):
        if gid != wid:
            assert abs(want_sim.get(gid, -9) - ws) <= 1e-6, (gid, wid)
        assert abs(gs - want_sim.get(gid, ws)) <= 1e-4


def test_chroma_search_matches_numpy_cosine_reference(tmp_path: Path) -> None:
    dim, n = 32, 400
    matrix = _vectors(n, dim)
    vs = memory.VectorStore(tmp_path / "chroma", "test/model", dim)
    vs.store(REPO, [str(i) for i in range(n)], matrix,
             [{"repo": REPO, "kind": "adopted", "rule_id": "R"}] * n)  # fmt: skip
    for q in _vectors(25, dim, seed=11):
        got = [(h.record_id, h.similarity) for h in vs.search(REPO, q, 10)]
        _same_ranking(got, _reference(matrix, q, 10))


def test_nearest_item_is_found_among_many_near_duplicates(tmp_path: Path) -> None:
    """같은 모양의 문서 절 수백 개 사이의 한 절. 근사 검색의 기본값은 이것을 놓쳤다
    (memory.HNSW)."""
    memo = (
        "## 운영 메모 {n}\n\n이 절은 규칙이 아니라 운영 기록이다. 배포 날짜와 담당자를 적어 둔다."
    )
    texts = [memo.replace("{n}", str(n)) for n in range(800)]
    texts.append(
        "## 주문 모듈\n\n- `orders/` 아래 모듈은 `print`를 쓰지 않는다. `orders/services/`도."
    )
    encoder = FakeEncoder()
    matrix = memory.embed(encoder, texts, role="passage").vectors
    q = memory.embed(encoder, ["바뀐 파일: orders/services/pricing.py\nprint(x)"], role="query")
    q = q.vectors[0]
    dim = FAKE_SPEC.dim
    vs = memory.VectorStore(tmp_path / "chroma", "test/model", dim)
    vs.store(REPO, [str(i) for i in range(len(matrix))], matrix,
             [{"repo": REPO, "kind": "adopted", "rule_id": "R"}] * len(matrix))  # fmt: skip
    got = [(h.record_id, h.similarity) for h in vs.search(REPO, q, 20)]
    _same_ranking(got, _reference(matrix, q, 20))
    assert got[0][0] == 800


def test_embed_normalizes_and_adds_the_role_prefix() -> None:
    encoder = FakeEncoder()
    out = memory.embed(encoder, ["가나다라 마바사", "x"], role="passage")
    assert out.vectors.shape == (2, FAKE_SPEC.dim)
    assert np.allclose(np.linalg.norm(out.vectors, axis=1), 1.0)
    assert encoder.seen == ["passage: 가나다라 마바사", "passage: x"]
    memory.embed(encoder, ["q"], role="query")
    assert encoder.seen[-1] == "query: q"
    assert out.model == FAKE_SPEC.name and out.dim == FAKE_SPEC.dim


def test_embed_without_role_is_an_error() -> None:
    with pytest.raises(TypeError):
        memory.embed(FakeEncoder(), ["a"])  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        memory.embed(FakeEncoder(), ["a"], role="document")


def test_other_repos_collection_is_never_searched(tmp_path: Path) -> None:
    vs = memory.VectorStore(tmp_path / "chroma", "test/model", 4)
    same = np.array([[1, 0, 0, 0]], dtype=np.float32)
    vs.store("o/other", ["1"], same, [{"repo": "o/other", "kind": "adopted", "rule_id": "R"}])
    vs.store(REPO, ["2"], same * 0.5 + np.array([[0, 0.5, 0, 0]], dtype=np.float32),
             [{"repo": REPO, "kind": "adopted", "rule_id": "R"}])  # fmt: skip
    hits = vs.search(REPO, same[0], 10)
    assert [h.record_id for h in hits] == [2]


def test_collection_made_with_another_model_is_not_opened(tmp_path: Path) -> None:
    memory.VectorStore(tmp_path / "chroma", "model-a", 4).collection(REPO)
    with pytest.raises(memory.ReindexNeeded):
        memory.VectorStore(tmp_path / "chroma", "model-b", 4).collection(REPO)
    with pytest.raises(memory.ReindexNeeded):
        memory.VectorStore(tmp_path / "chroma", "model-a", 8).collection(REPO)


def _add(db: Store, n: int, repo: str = REPO, kind: str = "adopted") -> None:
    for i in range(n):
        db.add_record(NewRecord(
            repo=repo, kind=kind, origin="fix_pr", source_id=f"{repo}:{kind}:{i}",
            rule_id=f"R-{i % 3}", rule_text=f"규칙 {i % 3}: 비교에 숫자를 직접 쓰지 않는다",
            file=f"pkg/m{i}.py", line_text=f"if x > {i}:", context=f"def f{i}(x):\n    if x > {i}:",
            diff=f"-    if x > {i}:\n+    if x > LIMIT_{i}:",
        ))  # fmt: skip


def test_index_pending_fills_embedded_at_once(tmp_path: Path) -> None:
    db = _store(tmp_path)
    _add(db, 5)
    vs = memory.VectorStore(tmp_path / "chroma", FAKE_SPEC.name, FAKE_SPEC.dim)
    assert memory.index_pending(db, vs, FakeEncoder(), REPO) == 5
    assert all(r.embedded_at is not None and r.embed_model == FAKE_SPEC.name
               for r in db.records(REPO))  # fmt: skip
    assert memory.index_pending(db, vs, FakeEncoder(), REPO) == 0
    assert vs.collection(REPO).count() == 5


def test_reindex_after_deleting_the_store_gives_the_same_results(tmp_path: Path) -> None:
    db = _store(tmp_path)
    _add(db, 12)
    _add(db, 3, repo="o/other")
    settings = RagSettings(enabled=True, min_similarity=0.0, max_records=3)
    vs = memory.VectorStore(tmp_path / "chroma", FAKE_SPEC.name, FAKE_SPEC.dim)
    query = [query_text("규칙 1: 비교에 숫자를 직접 쓰지 않는다", "def f4(x):\n    if x > 4:")]
    before = memory.Retriever(db, REPO, settings, vs, FakeEncoder()).find("decide", query)
    assert before.chosen

    import shutil

    vs.close()
    shutil.rmtree(tmp_path / "chroma")
    fresh = memory.VectorStore(tmp_path / "chroma", FAKE_SPEC.name, FAKE_SPEC.dim)
    assert memory.reindex(db, fresh, FakeEncoder()) == {"o/other": 3, REPO: 12}
    after = memory.Retriever(db, REPO, settings, fresh, FakeEncoder()).find("decide", query)
    assert after.ids == before.ids
    assert [round(c.similarity, 4) for c in after.chosen] == [
        round(c.similarity, 4) for c in before.chosen
    ]


def test_retriever_uses_only_its_repo_and_requested_kinds(tmp_path: Path) -> None:
    db = _store(tmp_path)
    _add(db, 4, kind="rejected")
    _add(db, 4, repo="o/other", kind="rejected")
    vs = memory.VectorStore(tmp_path / "chroma", FAKE_SPEC.name, FAKE_SPEC.dim)
    for repo in ("o/other",):
        memory.index_pending(db, vs, FakeEncoder(), repo)
    settings = RagSettings(enabled=True, min_similarity=0.0, max_records=10)
    retriever = memory.Retriever(db, REPO, settings, vs, FakeEncoder())
    query = [query_text("규칙 0: 비교에 숫자를 직접 쓰지 않는다", "if x > 0:")]
    found = retriever.find("decide", query)
    assert {c.record.repo for c in found.chosen} == {REPO}
    assert len(found.chosen) == 4
    assert retriever.find("fix", query).chosen == ()  # 수정 작성은 채택 기록만


def test_retriever_records_an_error_instead_of_raising(tmp_path: Path) -> None:
    db = _store(tmp_path)
    _add(db, 1)
    memory.VectorStore(tmp_path / "chroma", "another-model", FAKE_SPEC.dim).collection(REPO)
    vs = memory.VectorStore(tmp_path / "chroma", FAKE_SPEC.name, FAKE_SPEC.dim)
    retriever = memory.Retriever(db, REPO, RagSettings(enabled=True), vs, FakeEncoder())
    assert retriever.find("decide", ["q"]).chosen == ()
    assert "ReindexNeeded" in retriever.error


def _spec_for(tokenizer: bytes, onnx: bytes):
    def file(path: str, data: bytes) -> ModelFile:
        return ModelFile(path, hashlib.sha256(data).hexdigest(), len(data))

    from dataclasses import replace

    return replace(FAKE_SPEC, tokenizer=file("onnx/tokenizer.json", tokenizer),
                   onnx=file("onnx/model.onnx", onnx))  # fmt: skip


def test_ensure_model_downloads_once_and_checks_hashes(tmp_path: Path) -> None:
    spec = _spec_for(b"tok", b"onnx-bytes")
    calls: list[str] = []
    payload = {spec.tokenizer.path: b"tok", spec.onnx.path: b"onnx-bytes"}

    def download(url: str, dest: Path, file: ModelFile) -> None:
        assert url == spec.url(file) and url.startswith("https://huggingface.co/")
        calls.append(file.path)
        dest.write_bytes(payload[file.path])

    folder = memory.ensure_model(tmp_path / "models", spec, download)
    assert (folder / "model.onnx").read_bytes() == b"onnx-bytes"
    memory.ensure_model(tmp_path / "models", spec, download)
    assert calls == [spec.tokenizer.path, spec.onnx.path]


def test_download_rejects_wrong_hash_and_plain_http(tmp_path: Path, monkeypatch) -> None:
    file = ModelFile("onnx/model.onnx", hashlib.sha256(b"good").hexdigest(), 4)
    with pytest.raises(memory.SearchUnavailable):
        memory._download("http://example.invalid/model.onnx", tmp_path / "m", file)

    class _Response:
        def __init__(self, data: bytes) -> None:
            self.data = data

        def read(self, n: int) -> bytes:
            out, self.data = self.data[:n], self.data[n:]
            return out

        def __enter__(self):
            return self

        def __exit__(self, *a) -> None:
            return None

    class _Opener:
        def __init__(self, data: bytes) -> None:
            self.data = data

        def open(self, url: str, timeout: int):
            return _Response(self.data)

    monkeypatch.setattr(memory.urllib.request, "build_opener", lambda *a: _Opener(b"evil"))
    with pytest.raises(memory.SearchUnavailable):
        memory._download("https://huggingface.co/x", tmp_path / "m", file)
    assert not (tmp_path / "m").exists()
    assert list(tmp_path.iterdir()) == []
    monkeypatch.setattr(memory.urllib.request, "build_opener", lambda *a: _Opener(b"good-and-more"))
    with pytest.raises(memory.SearchUnavailable):
        memory._download("https://huggingface.co/x", tmp_path / "m", file)
    monkeypatch.setattr(memory.urllib.request, "build_opener", lambda *a: _Opener(b"good"))
    memory._download("https://huggingface.co/x", tmp_path / "m", file)
    assert (tmp_path / "m").read_bytes() == b"good"


def test_open_retriever_without_the_model_keeps_checking_without_search(tmp_path: Path) -> None:
    def fail(url: str, dest: Path, file: ModelFile) -> None:
        raise memory.SearchUnavailable("네트워크 없음")

    db = _store(tmp_path)
    retriever = memory.open_retriever(tmp_path, db, REPO, RagSettings(enabled=True), FAKE_SPEC,
                                      download=fail)  # fmt: skip
    assert retriever.find("decide", ["q"]).chosen == ()
    assert "네트워크 없음" in retriever.error


def test_importing_the_worker_does_not_import_the_search_libraries() -> None:
    """검색을 켜지 않은 워커는 chromadb·onnxruntime·tokenizers·numpy를 import하지 않는다 (5-8).
    이 모듈들은 app/worker/memory.py의 함수 안에서만 import한다."""
    code = (
        "import sys\n"
        "import app.worker.main, app.worker.graph, app.worker.update, app.worker.command\n"
        "import app.worker.memory, app.worker.cli\n"
        "heavy = ('chromadb', 'onnxruntime', 'tokenizers', 'numpy')\n"
        "bad = [m for m in heavy if m in sys.modules]\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         cwd=Path(__file__).resolve().parents[2])  # fmt: skip
    assert out.stdout.strip() == ""


def test_marked_time_is_kept(tmp_path: Path) -> None:
    db = _store(tmp_path)
    _add(db, 1)
    at = datetime(2026, 11, 20, tzinfo=UTC)
    db.mark_embedded([db.records(REPO)[0].id], "m", at)
    assert db.pending_records(REPO) == []
    db.clear_embedded(REPO)
    assert len(db.pending_records(REPO)) == 1
