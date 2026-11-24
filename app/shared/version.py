"""지금 도는 파이프라인 코드의 커밋과 패키지의 판.

커밋은 검사 기록(runs)에 남기고, 동기화가 이 값으로 "옛 코드가 낸 결과"를 가려 다시 검사한다
(docs/DESIGN.md 4.1 "끝난 상태별 재등록 규칙"). 판은 결과 코멘트의 표시에 실어, 같은 저장소를 보는
다른 서버와 견준다(`other_server_pure`).
"""

import json
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


@cache
def package() -> str:
    """패키지 폴더의 `package.json`에 적힌 판(`pr-proof update`가 견주는 값). 읽지 못하면 빈
    문자열이다(견줄 때 "같음"으로 본다). 커밋과 같이 프로세스마다 한 번만 읽는다."""
    try:
        data = json.loads((REPO_ROOT / "package.json").read_text())
    except (OSError, ValueError):
        return ""
    found = data.get("version") if isinstance(data, dict) else None
    return found.strip() if isinstance(found, str) else ""
