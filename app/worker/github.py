"""GitHub 쓰기 (gh CLI): PR 코멘트 하나를 달고 진행하면서 고친다."""

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from app.models.comment import COMMENT_MARKER, REPLY_MARKER

GH_TIMEOUT: Final = 60
_ENV_PASSTHROUGH: Final = ("PATH", "HOME", "GH_CONFIG_DIR")

GhRunner = Callable[[list[str], str | None], str]


@dataclass(frozen=True)
class PullRefs:
    head_sha: str
    base_sha: str
    open: bool
    head_ref: str = ""  # PR 브랜치 이름. 제안 PR의 base가 된다
    head_repo: str = ""  # PR 브랜치가 있는 리포. 원본 리포와 다르면 포크 PR
    # PR 작성자와 저장소의 관계, 로그인 이름. 워커가 체크아웃 전에 이 값으로 실행을 정한다 (8.6)
    author_association: str = ""
    author_login: str = ""


@dataclass(frozen=True)
class PullStatus:
    open: bool
    merged: bool
    merge_commit: str | None


# 브랜치가 이미 없을 때 GitHub이 돌려주는 실패
_BRANCH_GONE: Final = ("Not Found", "404", "422", "Reference does not exist")


class GitHubError(RuntimeError):
    """gh 호출 실패. 로그인 만료·네트워크 문제일 수 있어 환경 오류로 다룬다."""


def run_gh(args: list[str], stdin: str | None = None) -> str:
    env = {k: os.environ[k] for k in _ENV_PASSTHROUGH if k in os.environ}
    try:
        proc = subprocess.run(
            ["gh", *args],
            input=stdin,
            capture_output=True,
            text=True,
            env=env,
            timeout=GH_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GitHubError(f"gh 실행 실패: {e}") from e
    if proc.returncode != 0:
        raise GitHubError(f"gh {' '.join(args[:2])} 실패: {proc.stderr.strip()[-500:]}")
    return proc.stdout


class GitHub:
    def __init__(self, runner: GhRunner = run_gh) -> None:
        self._run = runner
        self._login: str | None = None

    def login(self) -> str:
        if self._login is None:
            self._login = self._run(["api", "user", "--jq", ".login"], None).strip()
        return self._login

    def pr_refs(self, repo: str, pr_number: int) -> PullRefs:
        """PR의 지금 head·base SHA, 열림 여부, 작성자. 조회 한 번에서 같이 읽는다."""
        jq = (
            '[.head.sha, .base.sha, .state, .head.ref, (.head.repo.full_name // ""), '
            '(.author_association // ""), (.user.login // "")] | @tsv'
        )
        out = self._run(["api", f"repos/{repo}/pulls/{pr_number}", "--jq", jq], None)
        fields = (out.rstrip("\n").split("\t") + [""] * 4)[:7]
        head, base, state, head_ref, head_repo, association, login = fields
        return PullRefs(
            head_sha=head, base_sha=base, open=state == "open", head_ref=head_ref,
            head_repo=head_repo, author_association=association, author_login=login,
        )  # fmt: skip

    # --- 제안 PR (DESIGN.md F10) ---

    def identity(self) -> tuple[str, str]:
        """제안 커밋의 작성자: 로그인한 계정 이름과 GitHub noreply 주소.

        개인 메일 주소를 쓰지 않는다.
        """
        out = self._run(["api", "user", "--jq", "[.id, .login] | @tsv"], None)
        user_id, login = out.strip().split("\t")
        return login, f"{user_id}+{login}@users.noreply.github.com"

    def open_pr_for_branch(self, repo: str, branch: str) -> int | None:
        owner = repo.split("/")[0]
        out = self._run(
            ["api", f"repos/{repo}/pulls?state=open&head={owner}:{branch}",
             "--jq", ".[0].number // empty"],
            None,
        )  # fmt: skip
        return int(out.strip()) if out.strip() else None

    def create_pr(self, repo: str, base: str, head: str, title: str, body: str) -> int:
        payload = json.dumps({"title": title, "head": head, "base": base, "body": body})
        out = self._run(
            ["api", "-X", "POST", f"repos/{repo}/pulls", "--input", "-", "--jq", ".number"],
            payload,
        )
        return int(out.strip())

    def update_pr(self, repo: str, number: int, title: str, body: str) -> None:
        payload = json.dumps({"title": title, "body": body})
        self._run(
            ["api", "-X", "PATCH", f"repos/{repo}/pulls/{number}", "--input", "-",
             "--jq", ".number"],
            payload,
        )  # fmt: skip

    def pull_status(self, repo: str, number: int) -> PullStatus:
        jq = '[.state, .merged, (.merge_commit_sha // "")] | @tsv'
        out = self._run(["api", f"repos/{repo}/pulls/{number}", "--jq", jq], None)
        state, merged, commit = (out.rstrip("\n").split("\t") + [""])[:3]
        return PullStatus(
            open=state == "open", merged=merged == "true", merge_commit=commit or None
        )

    def close_pr(self, repo: str, number: int, reason: str) -> None:
        """이유를 코멘트로 남기고 닫는다."""
        payload = json.dumps({"body": f"{REPLY_MARKER}\n{reason}"})
        self._run(
            ["api", f"repos/{repo}/issues/{number}/comments", "--input", "-", "--jq", ".id"],
            payload,
        )
        self._run(
            ["api", "-X", "PATCH", f"repos/{repo}/pulls/{number}", "--input", "-",
             "--jq", ".number"],
            json.dumps({"state": "closed"}),
        )  # fmt: skip

    def delete_branch(self, repo: str, branch: str) -> None:
        """파이프라인 브랜치를 지운다. 이미 없으면 넘어간다."""
        try:
            self._run(["api", "-X", "DELETE", f"repos/{repo}/git/refs/heads/{branch}"], None)
        except GitHubError as e:
            if not any(code in str(e) for code in _BRANCH_GONE):
                raise

    def reply(self, repo: str, pr_number: int, body: str) -> int:
        """명령에 답하는 새 코멘트. 결과 코멘트와 따로 단다."""
        payload = json.dumps({"body": f"{REPLY_MARKER}\n{body}"})
        out = self._run(
            ["api", f"repos/{repo}/issues/{pr_number}/comments", "--input", "-", "--jq", ".id"],
            payload,
        )
        return int(out.strip())

    def find_comment(self, repo: str, pr_number: int) -> int | None:
        """이 계정이 단, 파이프라인 표시가 있는 코멘트의 ID."""
        jq = f'.[] | select(.body | contains("{COMMENT_MARKER}")) | [.id, .user.login] | @tsv'
        out = self._run(
            ["api", "--paginate", f"repos/{repo}/issues/{pr_number}/comments", "--jq", jq], None
        )
        me = self.login()
        for line in out.splitlines():
            comment_id, _, author = line.partition("\t")
            if author == me:
                return int(comment_id)
        return None

    def upsert_comment(self, repo: str, pr_number: int, body: str, comment_id: int | None) -> int:
        """코멘트를 고치거나, 없으면 단다. 본문에 파이프라인 표시를 붙인다."""
        payload = json.dumps({"body": f"{COMMENT_MARKER}\n{body}"})
        if comment_id is not None and self._patch(repo, comment_id, payload):
            return comment_id
        # 저장한 ID가 없거나 코멘트가 지워졌으면 다시 찾고, 그래도 없으면 새로 단다
        comment_id = self.find_comment(repo, pr_number)
        if comment_id is not None and self._patch(repo, comment_id, payload):
            return comment_id
        out = self._run(
            ["api", f"repos/{repo}/issues/{pr_number}/comments", "--input", "-", "--jq", ".id"],
            payload,
        )
        return int(out.strip())

    def _patch(self, repo: str, comment_id: int, payload: str) -> bool:
        """코멘트를 고친다. 코멘트가 없으면(404) False, 다른 실패는 GitHubError."""
        try:
            self._run(
                ["api", "-X", "PATCH", f"repos/{repo}/issues/comments/{comment_id}",
                 "--input", "-", "--jq", ".id"],
                payload,
            )  # fmt: skip
        except GitHubError as e:
            if "Not Found" in str(e) or "404" in str(e):
                return False
            raise
        return True
