"""GitHub 조회 (gh CLI). 웹 쪽 동기화가 쓴다. 쓰기는 워커가 한다 (app/worker/github.py)."""

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from app.shared import config, github_auth
from app.shared.feedback_pure import FeedbackKind

GH_TIMEOUT: Final = 60
_ENV_PASSTHROUGH: Final = ("PATH", "HOME", "GH_CONFIG_DIR")
PR_FIELDS: Final = "number,headRefOid,baseRefOid,headRefName,isDraft,author"

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
    # PR 작성자의 로그인 이름. 기록용으로 `PRJob`에 싣는다 (8.6). `gh pr list`는
    # author_association을 주지 않아 동기화로 만든 작업에는 그 값이 없다
    author_login: str = ""


def run_gh(args: list[str]) -> str:
    """gh를 최소 환경으로 부른다. 토큰 방식이면 토큰을 이 프로세스에만 넘긴다(`github_auth`)."""
    try:
        env = github_auth.gh_env(_ENV_PASSTHROUGH)
    except config.ConfigError as e:
        raise GitHubReadError(f"GitHub 토큰을 읽지 못함: {e}") from e
    try:
        proc = subprocess.run(
            ["gh", *args], capture_output=True, text=True, env=env, timeout=GH_TIMEOUT, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GitHubReadError(f"gh 실행 실패: {e}") from e
    if proc.returncode != 0:
        detail = github_auth.failure_detail(proc.stderr, proc.stdout, proc.returncode, env)
        raise GitHubReadError(f"gh {' '.join(args[:2])} 실패: {detail}")
    return proc.stdout


def auth_ok(runner: GhRunner = run_gh) -> bool:
    """`gh auth status`가 통과하는지. 네트워크가 끊겨도 실패한다(gh가 둘을 가르지 못한다).
    토큰 방식이면 토큰 계정만 본다(`github_auth.status_args`)."""
    try:
        runner(github_auth.status_args())
    except GitHubReadError:
        return False
    return True


@dataclass(frozen=True)
class PRComment:
    id: int
    author_association: str
    created_at: str  # ISO 8601 (UTC, "Z")
    body: str
    login: str = ""  # 명령 작성자. 기록용 (8.6)


def head_commit_time(repo: str, head_sha: str, runner: GhRunner = run_gh) -> str:
    """head 커밋의 커미터 시각 (ISO 8601). 놓친 명령은 이 시각 뒤에 달린 것만 처리한다."""
    out = runner(["api", f"repos/{repo}/commits/{head_sha}", "--jq", ".commit.committer.date"])
    return out.strip()


def list_comments_since(
    repo: str, pr_number: int, since: str, runner: GhRunner = run_gh
) -> list[PRComment]:
    """since 뒤에 달린 PR 코멘트. 본문은 여러 줄일 수 있어 한 줄에 JSON 하나로 받는다."""
    jq = '.[] | {id, author_association, created_at, body, login: (.user.login // "")} | @json'
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
                login=item.get("login") or "",
            )
        )
    return comments


@dataclass(frozen=True)
class FeedbackItem:
    """PR에 달린 코멘트 하나 (줄 리뷰 코멘트, 리뷰 본문, 일반 코멘트). 거르기는 호출한 쪽이 한다."""

    kind: FeedbackKind
    id: int
    login: str
    author_association: str
    body: str


# 읽는 곳과 그 응답에서 뺄 것. 줄 코멘트는 스레드의 첫 코멘트만(답글은 in_reply_to_id가 있다),
# 리뷰는 본문이 빈 것(줄 코멘트만 단 리뷰)을 뺀다
_FEEDBACK_SOURCES: Final[tuple[tuple[FeedbackKind, str, str], ...]] = (
    ("review_comment", "pulls/{n}/comments", "select(.in_reply_to_id == null)"),
    ("review", "pulls/{n}/reviews", 'select((.body // "") != "")'),
    ("issue_comment", "issues/{n}/comments", "select(true)"),
)


def list_feedback(repo: str, pr_number: int, runner: GhRunner = run_gh) -> list[FeedbackItem]:
    """PR에 달린 코멘트 전부(세 곳). 본문은 여러 줄일 수 있어 한 줄에 JSON 하나로 받는다.

    동기화가 처리하지 않은 코멘트가 있는지 볼 때 쓴다. 글쓴이와 표시는 걸러 오지 않는다.
    """
    items: list[FeedbackItem] = []
    for kind, path, keep in _FEEDBACK_SOURCES:
        jq = f'.[] | {keep} | {{id, author_association, body, login: (.user.login // "")}} | @json'
        out = runner(["api", "--paginate", f"repos/{repo}/{path.format(n=pr_number)}", "--jq", jq])
        for line in out.splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                items.append(
                    FeedbackItem(
                        kind=kind,
                        id=int(item["id"]),
                        login=item.get("login") or "",
                        author_association=item.get("author_association") or "",
                        body=item.get("body") or "",
                    )
                )
            except (ValueError, KeyError, TypeError) as e:
                raise GitHubReadError(f"코멘트 조회 응답을 읽지 못함: {repo}#{pr_number}") from e
    return items


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
            author_login=(pr.get("author") or {}).get("login") or "",
        )
        for pr in json.loads(out or "[]")
    ]
