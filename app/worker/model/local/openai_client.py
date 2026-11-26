"""로컬 OpenAI 호환 HTTP 서버 호출 (표준 라이브러리 `urllib.request`만 사용).

로컬 주소(`localhost`, `127.0.0.1`, `[::1]`)로만 요청하고 시스템 프록시를 우회한다.
"""

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Final

from app.shared.model_names_pure import is_local_openai_url
from app.worker.model.model_backend import AIErrorKind

# 호출 하나의 시간 상한. 로컬 모델은 컴퓨터에 따라 느려 생각과 답에 10분을 넘게 쓸 수 있다
DEFAULT_TIMEOUT_S: Final = 1200.0
MODELS_TIMEOUT_S: Final = 5.0
_MAX_ERR_CHARS: Final = 400
# 로컬 서버 호출은 시스템/환경변수 프록시를 타지 않는다
_OPENER: Final = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class LocalApiError(RuntimeError):
    """로컬 OpenAI 호환 서버 호출 실패."""

    def __init__(
        self,
        kind: AIErrorKind,
        detail: str,
        *,
        status: int | None = None,
        environment: bool = False,
    ) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail
        self.status = status
        self.environment = environment


def _ensure_local(base_url: str) -> str:
    cleaned = base_url.rstrip("/")
    if not is_local_openai_url(cleaned):
        raise ValueError(f"로컬 주소가 아님: {base_url}")
    return cleaned


def post_chat(
    base_url: str,
    payload: Mapping[str, object],
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> Mapping[str, object]:
    """`POST {base_url}/chat/completions`를 호출하고 응답 JSON 객체를 돌려준다."""
    url = f"{_ensure_local(base_url)}/chat/completions"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except TimeoutError as e:
        raise LocalApiError(AIErrorKind.TIMEOUT, f"로컬 모델 서버 시간 초과 ({timeout:g}초)") from e
    except urllib.error.HTTPError as e:
        snippet = e.read().decode("utf-8", errors="replace").strip()[:_MAX_ERR_CHARS]
        raise LocalApiError(
            AIErrorKind.EXIT,
            f"로컬 모델 서버 HTTP {e.code}: {snippet}",
            status=e.code,
            environment=e.code in (502, 503, 504),
        ) from e
    except urllib.error.URLError as e:
        if isinstance(e.reason, TimeoutError):
            raise LocalApiError(
                AIErrorKind.TIMEOUT, f"로컬 모델 서버 시간 초과 ({timeout:g}초)"
            ) from e
        raise LocalApiError(
            AIErrorKind.LAUNCH,
            f"로컬 모델 서버에 연결하지 못함 ({url}): {e.reason}",
            environment=True,
        ) from e
    except OSError as e:
        raise LocalApiError(
            AIErrorKind.LAUNCH,
            f"로컬 모델 서버 통신 실패 ({url}): {e}",
            environment=True,
        ) from e
    try:
        parsed = json.loads(raw)
    except ValueError as e:
        raise LocalApiError(
            AIErrorKind.BAD_JSON, f"로컬 모델 서버 응답이 JSON이 아님: {raw[:200]}"
        ) from e
    if not isinstance(parsed, dict):
        raise LocalApiError(AIErrorKind.BAD_JSON, "로컬 모델 서버 응답이 JSON 객체가 아님")
    return parsed


def list_models(base_url: str, *, timeout: float = MODELS_TIMEOUT_S) -> list[str] | None:
    """`GET {base_url}/models`로 서버가 살아 있는지 확인한다. 연결하지 못하면 None."""
    url = f"{_ensure_local(base_url)}/models"
    req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return []
    data = parsed.get("data") if isinstance(parsed, dict) else None
    if not isinstance(data, list):
        return []
    return [str(item["id"]) for item in data if isinstance(item, dict) and "id" in item]
