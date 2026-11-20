"""macOS 알림 한 줄 띄우기 (DESIGN.md 13절 6단계, 완료 기준 6-4).

웹(동기화)과 워커가 같이 쓴다. 사람이 해야 풀리는 환경 오류에만 부른다(`models.ops_alert`).
`osascript`의 `display notification`을 셸 없이 인자 목록으로 부르고, 글은 AppleScript 소스가 아니라
`on run argv`의 인자로 넘긴다. macOS가 아니거나 `osascript`가 없거나 실패하면 아무것도 하지 않는다.
같은 종류는 `data_dir/alerts/<종류>`의 시각으로 30분에 한 번만 띄운다. 표시가 파일이라 프로세스를
다시 띄워도, Redis가 꺼져도 센다. 무엇이 잘못돼도 예외를 올리지 않는다. 알림은 아무것도 고치지
않는다(다시 시작, Docker 켜기, 로그인을 하지 않는다).
"""

import fcntl
import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final, Literal

from app.models.ops_alert import MARKER_DIR, TEXT, WINDOW_SECONDS, Kind, due

OSASCRIPT: Final = Path("/usr/bin/osascript")
TIMEOUT_SECONDS: Final = 5
# 글은 소스에 넣지 않고 argv로 받는다. 글에 따옴표나 AppleScript가 있어도 문자열로만 보인다
_SCRIPT: Final = ("on run argv", "display notification (item 1 of argv)", "end run")
_ENV_PASSTHROUGH: Final = ("PATH", "HOME")
_LOCK: Final = ".lock"

Outcome = Literal["shown", "duplicate", "failed"]
Show = Callable[[str], bool]

log = logging.getLogger(__name__)


def show_notification(
    text: str,
    *,
    platform: str = sys.platform,
    osascript: Path = OSASCRIPT,
    run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> bool:
    """알림 한 줄을 띄운다. 띄웠으면 참. macOS가 아니거나 띄우지 못하면 거짓."""
    if platform != "darwin" or not osascript.is_file():
        return False
    args = [str(osascript)]
    for line in _SCRIPT:
        args += ["-e", line]
    args.append(text)
    env = {k: os.environ[k] for k in _ENV_PASSTHROUGH if k in os.environ}
    try:
        proc = run(args, capture_output=True, env=env, timeout=TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _last_shown(marker: Path) -> float | None:
    try:
        return float(marker.read_text().strip())
    except (OSError, ValueError):
        return None  # 없거나 읽지 못하면 띄운 적이 없는 것으로 본다


def send(
    kind: Kind,
    data_dir: Path,
    *,
    now: float | None = None,
    window_seconds: int = WINDOW_SECONDS,
    show: Show | None = None,
) -> Outcome:
    """이 종류의 알림을 띄운다. 같은 종류를 창 안에 이미 띄웠으면 띄우지 않는다.

    워커와 웹이 동시에 부를 수 있어 표시 폴더의 잠금 안에서 확인하고 띄우고 적는다. 띄우지 못하면
    표시를 적지 않아 다음 오류가 다시 띄운다.
    """
    folder = data_dir / MARKER_DIR
    marker = folder / kind
    show = show or show_notification
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / _LOCK, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            moment = time.time() if now is None else now
            if not due(_last_shown(marker), moment, window_seconds):
                return "duplicate"
            if not show(TEXT[kind]):
                return "failed"
            try:
                marker.write_text(f"{moment}\n")
            except OSError:
                log.warning("운영 알림: 표시를 적지 못해 다음 오류가 다시 띄운다 (%s)", kind)
    except OSError:
        log.warning("운영 알림: 표시 폴더를 쓰지 못해 띄우지 않음 (%s)", kind)
        return "failed"
    return "shown"
