"""지금 도는 파이프라인 코드의 커밋. 검사 기록(runs)에 남기고, 동기화가 이 값으로 "옛 코드가 낸
결과"를 가려 다시 검사한다 (docs/DESIGN.md 4.1 "끝난 상태별 재등록 규칙")."""

import os
import subprocess
from functools import cache
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[2]


@cache
def current() -> str:
    """프로세스가 처음 물었을 때의 `git rev-parse HEAD`. 알 수 없으면 빈 문자열이다.

    프로세스마다 한 번만 읽는다. 서버를 다시 띄우지 않고 폴더만 새 커밋으로 올려도 도는 코드는 옛
    것이므로, 다시 읽지 않는다.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True,
            env={"PATH": os.environ.get("PATH", "")}, check=False, timeout=10,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""
