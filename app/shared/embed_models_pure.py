"""과거 기록 검색이 받을 수 있는 로컬 임베딩 모델 (docs/DESIGN.md 6.5). 외부 호출 없음.

설정(`config.RagConfig`)이 이 목록으로 `rag.embed_model`을 확인하므로 웹과 워커가 같이 읽는다.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class ModelFile:
    path: str  # 모델 저장소 안의 경로
    sha256: str
    size: int


@dataclass(frozen=True)
class EmbedModel:
    """파이프라인이 받을 수 있는 로컬 임베딩 모델. 받는 곳, 판(revision), 파일의 SHA256과 크기를
    고정한다 (도구 캐시 `toolcache.TOOLS`와 같은 방식). 목록 밖의 모델은 받지 않는다."""

    name: str
    revision: str  # 모델 저장소의 커밋. 같은 이름이라도 이 판의 파일만 받는다
    dim: int
    query_prefix: str
    passage_prefix: str
    tokenizer: ModelFile
    onnx: ModelFile
    max_tokens: int = 512

    def url(self, file: ModelFile) -> str:
        return f"https://huggingface.co/{self.name}/resolve/{self.revision}/{file.path}"


E5_SMALL: Final = "intfloat/multilingual-e5-small"
EMBED_MODELS: Final[Mapping[str, EmbedModel]] = {
    E5_SMALL: EmbedModel(
        name=E5_SMALL,
        revision="614241f622f53c4eeff9890bdc4f31cfecc418b3",
        dim=384,
        query_prefix="query: ",
        passage_prefix="passage: ",
        tokenizer=ModelFile(
            "onnx/tokenizer.json",
            "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39",
            17_082_730,
        ),
        onnx=ModelFile(
            "onnx/model.onnx",
            "ca456c06b3a9505ddfd9131408916dd79290368331e7d76bb621f1cba6bc8665",
            470_268_510,
        ),
    ),
}
