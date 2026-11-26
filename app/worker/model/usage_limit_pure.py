"""claude CLI가 계정의 사용량 한도에 닿았는지 읽는다 (외부 호출 없음).

claude CLI는 stream-json 출력에 `rate_limit_event` 줄을 낸다. `rate_limit_info.status`가
"rejected"면 한도에 닿은 것이고 `resetsAt`(초 단위 epoch)이 한도가 풀리는 시각이다.
"""

import json
from dataclasses import dataclass
from typing import Final

REJECTED: Final = "rejected"
# 풀리는 시각을 받지 못했을 때 claude를 다시 불러 보기까지의 시간. 한도의 창은 시간 단위라, 그보다
# 훨씬 짧게 두어 풀린 뒤 오래 로컬 모델에 머물지 않게 한다
REPROBE_SECONDS: Final = 900.0


@dataclass(frozen=True)
class Limited:
    """사용량 한도에 닿음. resets_at은 풀리는 시각(초 단위 epoch)이고 모르면 None이다."""

    resets_at: float | None


def limited(stdout: str) -> Limited | None:
    """출력의 마지막 `rate_limit_event`가 한도에 닿았다고 하면 그 정보, 아니면 None."""
    last: dict | None = None
    for line in stdout.splitlines():
        if '"rate_limit_event"' not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "rate_limit_event":
            info = event.get("rate_limit_info")
            last = info if isinstance(info, dict) else None
    if last is None or last.get("status") != REJECTED:
        return None
    resets = last.get("resetsAt")
    return Limited(float(resets) if isinstance(resets, int | float) and resets > 0 else None)


def until(limit: Limited, now: float) -> float:
    """로컬 모델로 도는 끝 시각. 풀리는 시각이 지금보다 뒤면 그 시각, 모르면 다시 불러 볼 시각."""
    if limit.resets_at is not None and limit.resets_at > now:
        return limit.resets_at
    return now + REPROBE_SECONDS
