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


@dataclass(frozen=True)
class PRComment:
    id: int
    author_association: str
    created_at: str  # ISO 8601 (UTC, "Z")
    body: str


def head_commit_time(repo: str, head_sha: str, runner: GhRunner = run_gh) -> str:
    """head 커밋의 커미터 시각 (ISO 8601). 놓친 명령은 이 시각 뒤에 달린 것만 처리한다."""
    out = runner(["api", f"repos/{repo}/commits/{head_sha}", "--jq", ".commit.committer.date"])
    return out.strip()


def list_comments_since(
    repo: str, pr_number: int, since: str, runner: GhRunner = run_gh
) -> list[PRComment]:
    """since 뒤에 달린 PR 코멘트. 본문은 여러 줄일 수 있어 한 줄에 JSON 하나로 받는다."""
    jq = ".[] | {id, author_association, created_at, body} | @json"
    out = runner(
        ["api", "--paginate", f"repos/{repo}/issues/{pr_number}/comments?since={since}",
         "--jq", jq]
    )  # fmt: skip
    comments = []
    for line in out.splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if item["created_at"] < since:  # since는 수정 시각 기준이라 오래전에 단 코멘트도 온다
            continue
        comments.append(
            PRComment(
                id=item["id"],
                author_association=item.get("author_association") or "",
                created_at=item["created_at"],
                body=item.get("body") or "",
            )
        )
    return comments


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
