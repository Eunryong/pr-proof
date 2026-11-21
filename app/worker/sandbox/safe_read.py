"""샌드박스가 쓸 수 있는 파일을 호스트에서 안전하게 읽는다.

`.pipeline-out/` 아래 결과 파일(report.json, coverage.xml)과 worktree의 소스는 샌드박스 안의 PR
코드가 바꿀 수 있다. 심볼릭 링크를 따라가거나 FIFO에서 멈추지 않게 일반 파일만 읽는다.
"""

import os
import stat
from pathlib import Path


def read_regular_file(path: Path, *, max_bytes: int) -> bytes | None:
    """일반 파일만 max_bytes까지 읽는다. 링크·FIFO·장치·너무 큰 파일·읽기 실패는 None.

    O_NOFOLLOW로 마지막 경로의 심볼릭 링크를 거부하고, O_NONBLOCK으로 FIFO에서 멈추지 않는다.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            return None
        with os.fdopen(fd, "rb", closefd=False) as f:
            data = f.read(max_bytes + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    return data if len(data) <= max_bytes else None
