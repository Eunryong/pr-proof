"""GitHub 조회 (gh CLI). 웹 쪽 동기화가 쓴다. 쓰기는 워커가 한다 (worker/github.py)."""

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

GH_TIMEOUT: Final = 60
_ENV_PASSTHROUGH: Final = ("PATH", "HOME", "GH_CONFIG_DIR")
PR_FIELDS: Final = "number,headRefOid,baseRefOid,headRefName,isDraft"

GhRunner = Callable[[list[str]], str]


class GitHubReadError(RuntimeError):
    """gh 조회 실패. 이번 동기화는 건너뛰고 다음 주기에 다시 한다."""


@dataclass(frozen=True)
class OpenPR:
    number: int
    head_sha: str
    base_sha: str
    head_ref: str
    draft: bool


def run_gh(args: list[str]) -> str:
    env = {k: os.environ[k] for k in _ENV_PASSTHROUGH if k in os.environ}
    try:
        proc = subprocess.run(
            ["gh", *args], capture_output=True, text=True, env=env, timeout=GH_TIMEOUT, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GitHubReadError(f"gh 실행 실패: {e}") from e
    if proc.returncode != 0:
        raise GitHubReadError(f"gh {' '.join(args[:2])} 실패: {proc.stderr.strip()[-500:]}")
    return proc.stdout


def list_open_prs(repo: str, runner: GhRunner = run_gh) -> list[OpenPR]:
    out = runner(
        ["pr", "list", "--repo", repo, "--state", "open", "--limit", "200", "--json", PR_FIELDS]
    )
    return [
        OpenPR(
            number=pr["number"],
            head_sha=pr["headRefOid"],
            base_sha=pr["baseRefOid"],
            head_ref=pr["headRefName"],
            draft=pr["isDraft"],
        )
        for pr in json.loads(out or "[]")
    ]
