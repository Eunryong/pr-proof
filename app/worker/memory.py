"""과거 기록 검색의 임베딩과 벡터 저장소 (docs/DESIGN.md 6.5, 6.6의 2·3·5번).

- 2 임베딩(`embed`): 로컬 모델로 직접 벡터화하고 L2 정규화한다. 기록의 글을 밖으로 보내지 않는다.
- 3 저장(`VectorStore.store`), 5 top-k 검색(`VectorStore.search`): Chroma 임베디드
  (`chromadb.PersistentClient`, `data_dir/chroma/`). 저장소마다 컬렉션이 따로이고, 컬렉션은 임베딩
  함수 없이 코사인 거리로 만든다. 벡터 저장소에는 기록 id, 벡터, 메타데이터(저장소, 종류, 규칙
  id)만 넣는다. 원본은 SQLite의 `records`다. 지워지면 `reindex`로 다시 만든다.

chromadb, onnxruntime, tokenizers, numpy는 선택 의존성(`pip install pr-proof[rag]`)이라 이 모듈의
함수 안에서만 import한다. 검색이 꺼진 검사(`rag.enabled: off`, 기록이 기준에 못 미친 `auto`)는
`switch_for`(SQLite만 읽음) 밖의 이 모듈 함수를 부르지 않으므로 넷 다 import되지 않고 `chroma/`
폴더와 임베딩 모델도 만들어지지 않는다 (완료 기준 5-8).

임베딩 모델은 `models/embed_models.py`의 `EMBED_MODELS`가 고정한 판(revision)의 파일만 https로
받아 SHA256을 맞춰 본 뒤 `data_dir/models/`에 둔다 (위협 T30).
"""

import hashlib
import importlib.util
import json
import logging
import os
import tempfile
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol

from app.models.embed_models import EmbedModel, ModelFile
from app.models.rag import (
    EMPTY,
    SITE_KINDS,
    Hit,
    Selection,
    collection_name,
    merge_hits,
    select,
    text_of_record,
    with_prefix,
)
from app.models.rag_switch import (
    AUTO_MIN_ATTEMPTS,
    AUTO_MIN_BASELINE,
    AUTO_MIN_RECORDS,
    RagMode,
    RagSwitch,
    decide,
)
from app.models.repo_config import RagSettings
from app.worker.state import Store

log = logging.getLogger(__name__)

# 선택 의존성 `[rag]` (pyproject.toml). 검색을 켜야 할 때 있는지만 본다
RAG_MODULES: Final = ("chromadb", "onnxruntime", "tokenizers", "numpy")
CHROMA_DIR: Final = "chroma"
MODEL_DIR: Final = "models"
INDEX_BATCH: Final = 32
DOWNLOAD_TIMEOUT: Final = 60
_CHUNK: Final = 1 << 20
_VERIFIED: Final = ".verified.json"
# 컬렉션의 거리 함수와 근사 검색의 넓이. 기본값(ef_search 100, 이웃 16)은 비슷한 글이 몰린
# 컬렉션(같은 모양의 문서 절 수백 개)에서 가장 가까운 조각을 놓쳤다 (5단계 작업, 11.1)
HNSW: Final[Mapping[str, object]] = {
    "space": "cosine",
    "ef_search": 400,
    "ef_construction": 400,
    "max_neighbors": 32,
}


class SearchUnavailable(RuntimeError):
    """검색을 할 수 없다 (모델을 받지 못함, 컬렉션의 모델이 다름 등). 검사는 검색 없이 계속한다."""


class ReindexNeeded(SearchUnavailable):
    """컬렉션을 만든 임베딩 모델이나 차원이 지금 설정과 다르다. `python -m app.cli reindex`."""


# --- 임베딩 모델 받기 ---


class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        if not newurl.startswith("https://"):
            raise SearchUnavailable(f"https가 아닌 곳으로 넘김: {newurl.split('?')[0]}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(url: str, dest: Path, file: ModelFile) -> None:
    """url을 dest로 받는다. 크기가 고정한 값을 넘거나 해시가 다르면 지우고 SearchUnavailable."""
    if not url.startswith("https://"):
        raise SearchUnavailable(f"https가 아닌 주소: {url}")
    opener = urllib.request.build_opener(_HttpsOnly)
    digest, size = hashlib.sha256(), 0
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.")
    try:
        with os.fdopen(fd, "wb") as out, opener.open(url, timeout=DOWNLOAD_TIMEOUT) as response:
            while chunk := response.read(_CHUNK):
                size += len(chunk)
                if size > file.size:
                    raise SearchUnavailable(f"{file.path}: 고정한 크기({file.size})보다 큼")
                digest.update(chunk)
                out.write(chunk)
        if size != file.size or digest.hexdigest() != file.sha256:
            raise SearchUnavailable(f"{file.path}: 크기나 해시가 고정한 값과 다름")
        os.replace(tmp, dest)
    except OSError as e:
        raise SearchUnavailable(f"{file.path}를 받지 못함: {e}") from e
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


Downloader = Callable[[str, Path, ModelFile], None]


def model_folder(model_dir: Path, spec: EmbedModel) -> Path:
    return model_dir / f"{spec.name.replace('/', '--')}@{spec.revision[:12]}"


def ensure_model(model_dir: Path, spec: EmbedModel, download: Downloader = _download) -> Path:
    """고정한 판의 토크나이저와 ONNX 모델이 든 폴더. 없으면 받고 해시를 맞춰 본다.

    맞춰 본 뒤 남긴 표시(파일마다 해시와 크기)가 있고 크기가 같으면 다시 해시하지 않는다.
    """
    folder = model_folder(model_dir, spec)
    folder.mkdir(parents=True, exist_ok=True)
    marker = folder / _VERIFIED
    try:
        verified = json.loads(marker.read_text()) if marker.is_file() else {}
    except (OSError, ValueError):
        verified = {}
    for file in (spec.tokenizer, spec.onnx):
        local = folder / Path(file.path).name
        if (
            verified.get(file.path) == file.sha256
            and local.is_file()
            and not local.is_symlink()
            and local.stat().st_size == file.size
        ):
            continue
        log.info("임베딩 모델 파일을 받음: %s (%d바이트)", spec.url(file), file.size)
        download(spec.url(file), local, file)
        verified[file.path] = file.sha256
        marker.write_text(json.dumps(verified))
    return folder


# --- 2: 임베딩 ---


class Encoder(Protocol):
    """글 목록 → 벡터 목록. 정규화와 접두어는 `embed`가 맡는다. 테스트는 가짜를 넘긴다."""

    spec: EmbedModel

    def encode(self, texts: Sequence[str]) -> Any: ...


@dataclass(frozen=True)
class Embedded:
    vectors: Any  # numpy float32 (글 수, 차원), 행마다 길이 1
    model: str
    dim: int


def embed(encoder: Encoder, texts: Sequence[str], *, role: str) -> Embedded:
    """글 목록을 정규화한 벡터로 (6.6의 2번). role은 `passage`(저장)나 `query`(질의)이고 생략할 수
    없다. 모델이 요구하는 접두어는 여기서 붙인다."""
    import numpy as np

    prefixed = with_prefix(encoder.spec, texts, role)
    if not prefixed:
        return Embedded(np.zeros((0, encoder.spec.dim), dtype=np.float32), encoder.spec.name,
                        encoder.spec.dim)  # fmt: skip
    raw = np.asarray(encoder.encode(prefixed), dtype=np.float32)
    if raw.ndim != 2 or raw.shape != (len(prefixed), encoder.spec.dim):
        raise SearchUnavailable(f"임베딩의 모양이 다름: {raw.shape}")
    norms = np.linalg.norm(raw, axis=1, keepdims=True)
    return Embedded(raw / np.clip(norms, 1e-12, None), encoder.spec.name, encoder.spec.dim)


class OnnxEncoder:
    """ONNX 모델과 토크나이저로 평균 풀링 벡터를 만든다 (e5 계열의 방식). 처음 쓸 때 연다."""

    def __init__(self, spec: EmbedModel, folder: Path) -> None:
        self.spec, self.folder = spec, folder
        self._session: Any = None
        self._tokenizer: Any = None

    def _load(self) -> None:
        import onnxruntime
        from tokenizers import Tokenizer

        tokenizer = Tokenizer.from_file(str(self.folder / Path(self.spec.tokenizer.path).name))
        tokenizer.enable_truncation(max_length=self.spec.max_tokens)
        pad = tokenizer.token_to_id("<pad>")
        tokenizer.enable_padding(pad_id=pad if pad is not None else 0)
        options = onnxruntime.SessionOptions()
        options.log_severity_level = 3
        self._session = onnxruntime.InferenceSession(
            str(self.folder / Path(self.spec.onnx.path).name), options,
            providers=["CPUExecutionProvider"],
        )  # fmt: skip
        self._tokenizer = tokenizer

    def encode(self, texts: Sequence[str]) -> Any:
        import numpy as np

        if self._session is None:
            self._load()
        encoded = self._tokenizer.encode_batch(list(texts))
        ids = np.array([e.ids for e in encoded], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
        feeds = {"input_ids": ids, "attention_mask": mask}
        if any(i.name == "token_type_ids" for i in self._session.get_inputs()):
            feeds["token_type_ids"] = np.zeros_like(ids)
        hidden = self._session.run(None, feeds)[0]
        weights = mask[..., None].astype(np.float32)
        return (hidden * weights).sum(axis=1) / np.clip(weights.sum(axis=1), 1e-9, None)


# --- 3, 5: 벡터 저장소 ---


class VectorStore:
    """`data_dir/chroma/`의 Chroma 임베디드. 저장소마다 컬렉션이 따로다(`collection_name`).

    컬렉션 메타데이터에 임베딩 모델 이름과 차원을 적고, 열 때 지금 설정과 다르면 쓰지 않는다
    (`ReindexNeeded`). 컬렉션은 임베딩 함수 없이 만들고 거리 함수는 코사인이다 (chromadb 1.5.9의
    `configuration={"hnsw": {"space": "cosine"}}`, 5단계 스파이크).
    """

    def __init__(self, path: Path, model: str, dim: int) -> None:
        self.path, self.model, self.dim = path, model, dim
        self._client: Any = None

    def client(self) -> Any:
        if self._client is None:
            import chromadb
            from chromadb.config import Settings

            self.path.mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(
                path=str(self.path), settings=Settings(anonymized_telemetry=False)
            )
        return self._client

    def close(self) -> None:
        """이 프로세스가 연 Chroma를 닫는다. chromadb는 같은 경로의 클라이언트를 프로세스 안에서
        재사용하므로, 폴더를 지운 뒤 같은 프로세스에서 다시 열려면 먼저 닫아야 한다."""
        if self._client is not None:
            from chromadb.api.client import SharedSystemClient

            SharedSystemClient.clear_system_cache()
            self._client = None

    def collection(self, repo: str, kind: str = "records") -> Any:
        name = collection_name(repo, "docs" if kind == "docs" else "records")
        col = self.client().get_or_create_collection(
            name,
            embedding_function=None,
            metadata={"embed_model": self.model, "dim": self.dim},
            configuration={"hnsw": dict(HNSW)},
        )
        meta = col.metadata or {}
        if meta.get("embed_model") != self.model or meta.get("dim") != self.dim:
            raise ReindexNeeded(
                f"컬렉션 {name}의 임베딩 모델이 다름 ({meta.get('embed_model')}, "
                f"{meta.get('dim')}차원). `python -m app.cli reindex`로 다시 만드세요"
            )
        return col

    def store(
        self,
        repo: str,
        ids: Sequence[str],
        vectors: Any,
        metadatas: Sequence[dict[str, str]],
        *,
        kind: str = "records",
    ) -> None:
        """id를 덮어쓴다 (6.6의 3번). 글과 diff는 넣지 않는다."""
        if not ids:
            return
        self.collection(repo, kind).upsert(
            ids=list(ids), embeddings=[list(map(float, v)) for v in vectors],
            metadatas=list(metadatas),
        )  # fmt: skip

    def search(self, repo: str, vector: Any, k: int) -> list[Hit]:
        """기록 컬렉션에서 질의 벡터와 가까운 순서로 k개 (6.6의 5번). 유사도는 1 - 코사인 거리."""
        return [Hit(int(i), s) for i, s in self.search_ids(repo, vector, k, kind="records")]

    def search_ids(self, repo: str, vector: Any, k: int, *, kind: str) -> list[tuple[str, float]]:
        """(id, 유사도). 문서 조각 컬렉션은 id가 조각의 해시다."""
        col = self.collection(repo, kind)
        n = min(k, col.count())
        if n <= 0:
            return []
        found = col.query(query_embeddings=[list(map(float, vector))], n_results=n,
                          include=["distances"])  # fmt: skip
        return [
            (str(i), 1.0 - float(d))
            for i, d in zip(found["ids"][0], found["distances"][0], strict=True)
        ]

    def known_ids(self, repo: str, *, kind: str) -> set[str]:
        return set(self.collection(repo, kind).get(include=[])["ids"])

    def drop(self, repo: str, kind: str = "records") -> None:
        name = collection_name(repo, "docs" if kind == "docs" else "records")
        try:
            self.client().delete_collection(name)
        except Exception:  # noqa: BLE001 (chromadb의 "없음" 예외는 판마다 이름이 다르다)
            log.info("지울 컬렉션이 없음: %s", name)


# --- 밀린 기록 임베딩, reindex ---


def index_pending(db: Store, vectors: VectorStore, encoder: Encoder, repo: str) -> int:
    """`embedded_at`이 빈 이 저장소의 기록을 임베딩해 넣고 시각과 모델을 적는다. 넣은 수."""
    done = 0
    pending = db.pending_records(repo)
    for start in range(0, len(pending), INDEX_BATCH):
        batch = pending[start : start + INDEX_BATCH]
        embedded = embed(encoder, [text_of_record(r) for r in batch], role="passage")
        vectors.store(
            repo, [str(r.id) for r in batch], embedded.vectors,
            [{"repo": r.repo, "kind": r.kind, "rule_id": r.rule_id} for r in batch],
        )  # fmt: skip
        db.mark_embedded([r.id or 0 for r in batch], encoder.spec.name, datetime.now(UTC))
        done += len(batch)
    return done


def reindex(
    db: Store, vectors: VectorStore, encoder: Encoder, repos: Iterable[str] | None = None
) -> dict[str, int]:
    """SQLite의 기록으로 벡터 저장소를 다시 만든다 (`python -m app.cli reindex`). 저장소마다
    컬렉션을 지우고 `embedded_at`을 비운 뒤 모두 다시 넣는다. 문서 조각 컬렉션도 지운다(다음 검사가
    다시 만든다). {저장소: 넣은 수}."""
    out = {}
    for repo in sorted(set(repos) if repos is not None else db.record_repos()):
        vectors.drop(repo, "records")
        vectors.drop(repo, "docs")
        db.clear_embedded(repo)
        out[repo] = index_pending(db, vectors, encoder, repo)
    return out


# --- 검색을 켤지 (6.7 "검색을 켜는 조건") ---


def missing_extras(modules: Sequence[str] = RAG_MODULES) -> bool:
    """선택 의존성 `[rag]`가 없는지. 모듈을 import하지 않고 찾기만 한다."""
    for name in modules:
        try:
            if importlib.util.find_spec(name) is None:
                return True
        except (ImportError, ValueError):
            return True
    return False


def switch_for(
    store: Store,
    repo: str,
    mode: RagMode,
    *,
    min_records: int = AUTO_MIN_RECORDS,
    min_attempts: int = AUTO_MIN_ATTEMPTS,
    min_baseline: int = AUTO_MIN_BASELINE,
    extras_missing: Callable[[], bool] = missing_extras,
) -> RagSwitch:
    """이 검사의 검색 상태. auto는 SQLite의 숫자만 읽는다(임베딩 모델과 Chroma를 열지 않는다).
    auto가 채택률 비교로 끄면 그 사실을 남겨 워커를 다시 띄워도 꺼진 채다. 켜야 하는데 선택
    의존성이 없으면 검색 없이 검사하도록 표시한다."""
    evidence = turned_off = None
    if mode == "auto":
        turned_off = store.rag_turned_off(repo, min_attempts, min_baseline)
        evidence = store.rag_evidence(repo) if turned_off is None else None
    switch = decide(mode, evidence, turned_off, min_records=min_records,
                    min_attempts=min_attempts, min_baseline=min_baseline)  # fmt: skip
    if switch.why == "worse" and turned_off is None and switch.evidence is not None:
        store.turn_rag_off(repo, switch.evidence, min_attempts, min_baseline)
        log.info("auto가 검색을 끔 %s: %s", repo, switch.evidence)
    if switch.wanted and extras_missing():
        return replace(switch, missing_extras=True)
    return switch


# --- 쓰는 곳의 입구 (6.6의 4–6번) ---


class FindsRecords(Protocol):
    """쓰는 곳(수정 판단, 수정 작성, 검사기 작성)이 보는 검색. 테스트는 가짜를 넘긴다."""

    error: str

    def find(self, site: str, queries: Sequence[str]) -> Selection: ...


class Retriever:
    """한 검사의 검색. 처음 쓸 때 밀린 기록을 임베딩하고(`prepare`), 쓰는 곳마다 질의를 받아 넣을
    기록을 고른다. 검색이 실패해도 검사를 멈추지 않는다: 빈 선택과 이유(`error`)를 남긴다."""

    def __init__(
        self, db: Store, repo: str, settings: RagSettings, vectors: VectorStore, encoder: Encoder
    ) -> None:
        self.db, self.repo, self.settings = db, repo, settings
        self.vectors, self.encoder = vectors, encoder
        self.error = ""
        self.indexed = 0
        self._prepared = False

    def prepare(self) -> None:
        if self._prepared:
            return
        self._prepared = True
        try:
            self.indexed = index_pending(self.db, self.vectors, self.encoder, self.repo)
        except Exception as e:  # noqa: BLE001 (모델·Chroma의 실패는 검사를 멈추지 않는다)
            self.error = _short(e)
            log.warning("밀린 기록을 임베딩하지 못함 %s: %s", self.repo, self.error)

    def find_sections(
        self, chunks: Sequence[tuple[str, str]], query: str
    ) -> list[tuple[str, float]]:
        """큰 규칙 문서의 조각 (6.3). chunks는 (조각 id, 임베딩할 글)이다. 이 저장소의 문서
        컬렉션에 없는 조각만 임베딩해 넣고(조각 id는 내용 해시라 바뀐 조각만 새로 넣는다), 질의와
        가까운 조각의 (id, 유사도)를 top-k로 돌려준다. 지금 문서에 없는 옛 조각은 버린다. 실패하면
        예외."""
        if self.error:
            raise SearchUnavailable(self.error)
        known = self.vectors.known_ids(self.repo, kind="docs")
        new = [(i, t) for i, t in dict(chunks).items() if i not in known]
        for start in range(0, len(new), INDEX_BATCH):
            batch = new[start : start + INDEX_BATCH]
            embedded = embed(self.encoder, [t for _, t in batch], role="passage")
            self.vectors.store(self.repo, [i for i, _ in batch], embedded.vectors,
                               [{"repo": self.repo, "kind": "doc"}] * len(batch),
                               kind="docs")  # fmt: skip
        current = {i for i, _ in chunks}
        vector = embed(self.encoder, [query], role="query").vectors[0]
        found = self.vectors.search_ids(self.repo, vector, self.settings.top_k, kind="docs")
        return [(i, s) for i, s in found if i in current]

    def find(self, site: str, queries: Sequence[str]) -> Selection:
        """queries(질의 글) 각각의 top-k를 합쳐 site가 쓰는 종류만 기준값으로 고른다."""
        self.prepare()
        if self.error or not queries or self.settings.max_records <= 0:
            return EMPTY
        try:
            embedded = embed(self.encoder, queries, role="query")
            hits = merge_hits(
                self.vectors.search(self.repo, v, self.settings.top_k) for v in embedded.vectors
            )
            records = {r.id: r for r in self.db.records_by_ids([h.record_id for h in hits])}
        except Exception as e:  # noqa: BLE001
            self.error = _short(e)
            log.warning("검색하지 못함 %s: %s", self.repo, self.error)
            return EMPTY
        return select(
            hits, records, repo=self.repo, kinds=SITE_KINDS[site],
            min_similarity=self.settings.min_similarity, limit=self.settings.max_records,
        )  # fmt: skip


def open_retriever(
    data_dir: Path, db: Store, repo: str, settings: RagSettings, spec: EmbedModel,
    download: Downloader = _download,
) -> Retriever:  # fmt: skip
    """검색이 켜진 검사(`switch_for`)의 검색. 임베딩 모델을 받고(처음 한 번) 벡터 저장소를 연다.
    받지 못하면 검색 없이 계속하도록 error가 채워진 Retriever를 돌려준다."""
    vectors = VectorStore(data_dir / CHROMA_DIR, spec.name, spec.dim)
    try:
        folder = ensure_model(data_dir / MODEL_DIR, spec, download)
    except SearchUnavailable as e:
        retriever = Retriever(db, repo, settings, vectors, _Unavailable(spec))
        retriever.error = _short(e)
        retriever._prepared = True
        return retriever
    return Retriever(db, repo, settings, vectors, OnnxEncoder(spec, folder))


class _Unavailable:
    def __init__(self, spec: EmbedModel) -> None:
        self.spec = spec

    def encode(self, texts: Sequence[str]) -> Any:
        raise SearchUnavailable("임베딩 모델이 없음")


def _short(e: BaseException) -> str:
    text = str(e).strip().splitlines()
    return f"{type(e).__name__}: {text[0][:200] if text else ''}"
