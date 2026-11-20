"""Slack 운영 알림의 종류와 글 (DESIGN.md 13절 6단계, v1 F12, 완료 기준 6-4). 외부 호출 없음.

알림에는 오류의 종류, 저장소#번호, 종류마다 정한 짧은 글만 넣는다. 예외 메시지, PR의 글, 저장소
파일, 모델의 출력, 비밀값은 넣지 않는다. 같은 (종류, 저장소)는 `WINDOW_SECONDS`에 한 번만 보낸다.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from app.models import state_keys

Kind = Literal[
    "env_error_final",  # 환경 오류로 작업의 재시도를 모두 씀
    "pipeline_error",  # 환경 오류가 아닌 예외로 작업의 재시도를 모두 씀
    "preflight_failed",  # 워커가 시작 전 확인(gh·claude 로그인, Docker, Redis)에서 멈춤
    "secret_blocked",  # 수정안·설명에 비밀값으로 보이는 것이 있어 버림 (위협 T21)
    "tool_denied",  # 모델이 허용 목록 밖의 도구를 부르려 함 (위협 T25)
    "sync_failed",  # 동기화가 저장소의 PR 목록을 읽지 못함
    "worker_lost",  # 실행 중이던 작업의 생존 확인이 끊겨 동기화가 다시 넣음
]

TEXT: Final[Mapping[str, str]] = {
    "env_error_final": "환경 오류로 재시도를 모두 썼습니다. Docker, gh·claude 로그인을 확인하세요.",
    "pipeline_error": "파이프라인 오류로 작업이 끝났습니다. 워커 로그를 확인하세요.",
    "preflight_failed": "워커가 시작 전 확인에서 멈췄습니다. 워커 로그를 확인하세요.",
    "secret_blocked": "수정안에 비밀값으로 보이는 내용이 있어 올리지 않았습니다.",
    "tool_denied": "모델이 허용되지 않은 도구를 부르려 했습니다. ai_calls를 확인하세요.",
    "sync_failed": "동기화가 저장소의 PR 목록을 읽지 못했습니다. gh 로그인을 확인하세요.",
    "worker_lost": "워커가 작업 중에 멈춰 동기화가 작업을 다시 넣었습니다.",
}
WINDOW_SECONDS: Final = 30 * 60
# GitHub 저장소 이름에 쓸 수 있는 글자. 그 밖이면 알림에 넣지 않는다
_REPO: Final = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")


@dataclass(frozen=True)
class Alert:
    kind: Kind
    repo: str | None = None
    pr_number: int | None = None

    @property
    def safe_repo(self) -> str | None:
        return self.repo if self.repo and _REPO.fullmatch(self.repo) else None

    @property
    def dedup_key(self) -> str:
        """같은 알림의 단위는 (종류, 저장소)다. 같은 원인(Docker가 꺼짐)으로 PR 여럿이 실패해도 한
        번만 온다."""
        return state_keys.ops_alert(self.kind, self.safe_repo)


def message(alert: Alert) -> str:
    where = ""
    if alert.safe_repo:
        where = f" · {alert.safe_repo}"
        if alert.pr_number is not None and alert.pr_number > 0:
            where += f"#{alert.pr_number}"
    return f"pr-proof 운영 알림 · {alert.kind}{where} · {TEXT[alert.kind]}"
