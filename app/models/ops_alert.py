"""운영 알림의 종류와 글 (DESIGN.md 13절 6단계, 완료 기준 6-4). 외부 호출 없음.

알리는 것은 사람이 해야 풀리는 환경 오류뿐이다. 종류는 워커의 시작 전 확인(preflight)이 가르는
네 가지이고, 글은 종류마다 정한 한 줄(할 일 포함)이다. 저장소 이름, 예외 메시지, PR의 글, 저장소
파일, 모델의 출력, 비밀값은 넣지 않는다. 같은 종류는 `WINDOW_SECONDS`에 한 번만 띄운다.
"""

from collections.abc import Mapping
from typing import Final, Literal

Kind = Literal["gh_login", "claude_login", "docker", "redis"]

TEXT: Final[Mapping[Kind, str]] = {
    "gh_login": "pr-proof: GitHub 로그인 확인이 실패했습니다. "
    "터미널에서 gh auth status를 실행하세요.",
    "claude_login": "pr-proof: Claude 로그인이 풀렸습니다. "
    "터미널에서 claude를 실행해 /login 하세요.",
    "docker": "pr-proof: Docker가 꺼져 있습니다. Docker Desktop을 실행하세요.",
    "redis": "pr-proof: Redis에 연결하지 못했습니다. "
    "pr-proof 폴더에서 docker compose up -d를 실행하세요.",
}
WINDOW_SECONDS: Final = 30 * 60
# data_dir 아래 표시 폴더. 종류마다 마지막으로 띄운 시각(epoch 초)을 파일 하나에 적는다
MARKER_DIR: Final = "alerts"


def due(last_shown: float | None, now: float, window_seconds: int = WINDOW_SECONDS) -> bool:
    """이 종류를 지금 띄워도 되는지. 표시가 없거나 창이 지났으면 참.

    시계가 뒤로 가 마지막 시각이 지금보다 뒤면 창이 지난 것으로 본다(영영 막히지 않게).
    """
    if last_shown is None:
        return True
    elapsed = now - last_shown
    return not 0 <= elapsed < window_seconds
