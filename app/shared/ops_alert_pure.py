"""운영 알림의 종류와 글 (DESIGN.md 13절 6단계, 완료 기준 6-4). 외부 호출 없음.

알리는 것은 사람이 해야 풀리는 일이다.

- 환경 오류: 종류는 워커의 시작 전 확인(preflight)이 가르는 네 가지이고(GitHub 인증은 방식에 따라
  gh_login과 gh_token으로 글이 갈린다), 글은 종류마다 정한 한 줄(할 일 포함)이다. 같은 종류는
  `WINDOW_SECONDS`에 한 번만 띄운다.
- 같은 저장소를 보는 다른 pr-proof 서버(`other_server_pure`): 저장소를 목록에서 빼거나 판을
  올리라고 권한다. 글에는 허용 목록의 저장소 이름과 두 서버의 판만 넣는다. 같은 저장소·같은 서버
  짝은 `OTHER_SERVER_WINDOW_SECONDS`에 한 번만 띄운다.

예외 메시지, PR의 글, 저장소 파일, 모델의 출력, 비밀값은 넣지 않는다.
"""

import re
from collections.abc import Mapping
from typing import Final, Literal

Kind = Literal["gh_login", "gh_token", "claude_login", "local_model", "docker", "redis"]

TEXT: Final[Mapping[Kind, str]] = {
    "gh_login": "pr-proof: GitHub 로그인 확인이 실패했습니다. "
    "터미널에서 gh auth status를 실행하세요.",
    "gh_token": "pr-proof: GitHub 토큰이 거부됐습니다. 만료되거나 폐기됐으면 새 토큰을 만들어 "
    "설정 폴더의 github_token 파일을 바꾸세요.",
    "claude_login": "pr-proof: Claude 로그인이 풀렸습니다. "
    "터미널에서 claude를 실행해 /login 하세요.",
    "local_model": "pr-proof: 로컬 모델 서버에 연결하지 못했습니다. "
    "Ollama 또는 llama-server가 실행 중인지 확인하세요.",
    "docker": "pr-proof: Docker가 꺼져 있습니다. Docker Desktop을 실행하세요.",
    "redis": "pr-proof: Redis에 연결하지 못했습니다. "
    "pr-proof 폴더에서 docker compose up -d를 실행하세요.",
}
WINDOW_SECONDS: Final = 30 * 60
# data_dir 아래 표시 폴더. 종류마다 마지막으로 띄운 시각(epoch 초)을 파일 하나에 적는다
MARKER_DIR: Final = "alerts"
# 다른 서버를 권하는 글은 고칠 때까지 같은 글이 되풀이되므로 하루에 한 번이다
OTHER_SERVER_WINDOW_SECONDS: Final = 24 * 60 * 60
# 명령 앞머리. 워커는 입구가 불린 방식을 모르므로 전역 설치의 이름을 쓴다
COMMAND: Final = "pr-proof"


def due(last_shown: float | None, now: float, window_seconds: int = WINDOW_SECONDS) -> bool:
    """이 종류를 지금 띄워도 되는지. 표시가 없거나 창이 지났으면 참.

    시계가 뒤로 가 마지막 시각이 지금보다 뒤면 창이 지난 것으로 본다(영영 막히지 않게).
    """
    if last_shown is None:
        return True
    elapsed = now - last_shown
    return not 0 <= elapsed < window_seconds


def other_server_key(repo: str, server_id: str) -> str:
    """같은 저장소·같은 서버 짝의 표시 파일 이름. 저장소 이름은 대소문자를 가리지 않는다."""
    safe = re.sub(r"[^0-9a-z._-]", "+", repo.lower())
    return f"other_server.{safe}.{server_id}"


def other_server_text(repo: str, mine: str, other: str, relation: int) -> str:
    """다른 서버를 권하는 글. relation은 다른 서버의 판을 이 서버의 판과 견준 값이다(-1 낮음, 0
    같음, 1 높음). 낮으면 이 서버가 넘겨받아 검사했고, 같거나 높으면 검사하지 않았다."""
    rm = f"`{COMMAND} rm {repo}`"
    if relation > 0:
        return (
            f"다른 pr-proof 서버(판 {other})가 {repo}를 처리하고 있어 이 서버(판 {mine})는 "
            f"검사하지 않았습니다. 이 서버에서 {rm}로 빼거나 `{COMMAND} update`로 판을 올리세요."
        )
    if relation < 0:
        return (
            f"판이 낮은 다른 pr-proof 서버(판 {other})도 {repo}를 처리하고 있습니다. 이 서버(판 "
            f"{mine})가 검사했습니다. 그 서버에서 {rm}로 빼거나 판을 올리세요."
        )
    return (
        f"다른 pr-proof 서버가 {repo}를 처리하고 있어 이 서버는 검사하지 않았습니다. "
        f"한 서버에서 {rm}로 빼세요."
    )
