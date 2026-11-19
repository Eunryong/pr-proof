"""검색 테스트의 가짜 임베딩 모델. 실제 모델(470MB)을 받지 않고 결정적으로 벡터를 만든다.

글자 3-gram을 해시해 센 벡터라 같은 낱말이 많은 글일수록 가깝다. 정규화하지 않은 값을 돌려줘
`memory.embed`가 정규화하는지도 본다. 받은 글(접두어 포함)을 `seen`에 남긴다.
"""

import hashlib
from collections.abc import Sequence
from typing import Final

from app.models.rag import EmbedModel, ModelFile

FAKE_DIM: Final = 256
FAKE_SPEC: Final = EmbedModel(
    name="test/fake-embed",
    revision="0" * 40,
    dim=FAKE_DIM,
    query_prefix="query: ",
    passage_prefix="passage: ",
    tokenizer=ModelFile("tokenizer.json", "0" * 64, 1),
    onnx=ModelFile("model.onnx", "0" * 64, 1),
)


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
