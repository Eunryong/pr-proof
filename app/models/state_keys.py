"""웹과 워커가 같이 쓰는 Redis 키 형식과 큐 이름. 읽기·쓰기 코드는 각자 자기 쪽에 둔다."""

from typing import Final

QUEUE_NAME: Final = "pipeline"
PREFIX: Final = "pipeline"

DELIVERY_TTL_SECONDS: Final = 24 * 60 * 60  # GitHub 재전송 웹훅을 건너뛰는 기간
JOB_TTL_SECONDS: Final = 6 * 60 * 60  # 워커가 죽어 키를 못 지워도 이 시간 뒤 다시 등록할 수 있다


def latest_head(repo: str, pr_number: int) -> str:
    """PR의 최신 head SHA. 웹이 쓰고 워커가 낡은 작업인지 확인할 때 읽는다."""
    return f"{PREFIX}:latest_head:{repo}#{pr_number}"


def job(dedup_key: str) -> str:
    """대기·실행 중인 작업 표시. 웹이 등록할 때 만들고 워커가 끝나면 지운다."""
    return f"{PREFIX}:job:{dedup_key}"


def delivery(delivery_id: str) -> str:
    """처리한 웹훅 delivery ID."""
    return f"{PREFIX}:delivery:{delivery_id}"


def ops_alert(kind: str, repo: str | None) -> str:
    """운영 알림을 보낸 표시. 있는 동안 같은 알림을 다시 보내지 않는다 (13절 6단계, 6-4)."""
    return f"{PREFIX}:ops_alert:{kind}:{repo or '-'}"
