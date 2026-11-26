"""모델 이름·백엔드 이름·로컬 서버 주소의 꼴.

저장소의 `.pipeline.yaml`(app/worker/config/repo_config_pure.py)과 파이프라인 쪽 설정
(app/shared/config.py), 설정 명령(app/setup/setup_pure.py), 모델 구현 고르기
(app/worker/model/backends.py)가 같이 쓴다. 외부 호출 없음.
"""

import re
from typing import Final

# `-`로 시작하면 CLI 옵션으로 읽히므로 막는다. Ollama·llama-server의 모델 이름(`hf.co/...:Q4_K_M`,
# `inkling-2-mini:latest`)을 받기 위해 `/`와 `:`를 허용한다.
MODEL_NAME_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9._:/\[\]-]*$"

CLAUDE_CLI_BACKEND: Final = "claude-cli"
LOCAL_OPENAI_BACKEND: Final = "local-openai"
KNOWN_BACKENDS: Final = (CLAUDE_CLI_BACKEND, LOCAL_OPENAI_BACKEND)
DEFAULT_LOCAL_OPENAI_URL: Final = "http://127.0.0.1:8080/v1"

# 로컬 서버 전용: http 스킴과 루프백 호스트(127.0.0.1, localhost, [::1])만 허용한다.
# 자격 증명(@), 쿼리(?), 프래그먼트(#), 공백은 거부한다.
_LOCAL_URL_RE: Final = re.compile(
    r"^http://(127\.0\.0\.1|localhost|\[::1\])(?::([0-9]{1,5}))?(?:/[^\s?#@]*)?$"
)


def is_local_openai_url(url: str) -> bool:
    """로컬 OpenAI 호환 서버 주소인지 확인한다 (localhost / 127.0.0.1 / [::1], http만)."""
    match = _LOCAL_URL_RE.fullmatch(url)
    if match is None:
        return False
    port = match.group(2)
    return port is None or 1 <= int(port) <= 65535
