"""모델 결과 캐시의 키 (docs/DESIGN.md 5.6, 9.1). 계산만 한다.

키는 단계, 모델, 프롬프트 버전, 그리고 단계마다 다른 입력 부분(원천 blob 해시, 규칙 원문 해시,
hunk 해시 등)의 해시다. 어느 하나가 바뀌면 키가 달라져 모델에 다시 묻는다.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence

Part = str | int | Sequence[str] | Sequence[Sequence[str]]


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def prompt_version(system_prompt: str, schema: Mapping[str, object], user_frame: str) -> str:
    """프롬프트 글과 스키마의 해시. 손으로 올리는 번호는 고치고도 잊을 수 있어 내용에서 만든다."""
    schema_text = json.dumps(schema, sort_keys=True, ensure_ascii=False)
    return text_hash("\0".join((system_prompt, schema_text, user_frame)))[:12]


def model_cache_key(stage: str, model: str, prompt_version: str, parts: Mapping[str, Part]) -> str:
    """parts의 이름 순서는 키에 영향을 주지 않고, 목록 안의 순서는 영향을 준다.

    단계·모델·프롬프트 버전이 비었으면 ValueError (빈 값끼리 같은 키가 되지 않게).
    """
    for name, value in (("stage", stage), ("model", model), ("prompt_version", prompt_version)):
        if not value:
            raise ValueError(f"캐시 키의 {name}이 비어 있다")
    payload = {
        "stage": stage,
        "model": model,
        "prompt_version": prompt_version,
        "parts": dict(parts),
    }
    # JSON으로 묶어 값의 경계가 섞이지 않게 한다 ("ab"+"c"와 "a"+"bc"가 다른 키)
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return text_hash(encoded)
