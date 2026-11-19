"""기본 자동 경로(require_approval: false)가 PR의 커밋마다 돌고, 제안 PR 기록을 지키는지.

- 새 커밋마다 같은 자동 경로가 다시 돈다 (이전 제안은 superseded로 닫힌다)
- 제안을 머지해 생긴 커밋은 그 제안을 다시 만들지 않는다 (루프 방지)
- 사람이 닫은 제안이 고친 위반은 줄 내용이 그대로면 다음 커밋에서도 고치지 않는다
"""

import dataclasses
import json
from typing import Final

import pytest

from app.models import state_keys
from app.models.finding import SkillResult
from app.models.job import PRJob
from app.models.repo_config import RepoConfig
from app.worker import base_config, main
from app.worker.skills import lint

from .helpers import BASE
from .test_fix_flow import FIXABLE, UNFIXABLE, fix_env  # noqa: F401

FIRST: Final = "a" * 40
SECOND: Final = "c" * 40


@pytest.fixture
def auto_env(fix_env, tmp_path):  # noqa: F811
    """기본 설정(자동) + 위반 줄의 실제 내용이 있는 worktree (a.py:2 F401)."""
    base = dataclasses.replace(BASE, repo=RepoConfig())
    fix_env["monkeypatch"].setattr(base_config, "load", lambda *a, **k: base)
    wt = tmp_path / "wt"
    wt.mkdir(parents=True, exist_ok=True)
    (wt / "a.py").write_text("x = 1\nimport sys\n\n\nl = 1\n")
    fix_env["wt"] = wt
    return fix_env


def _comment(env) -> str:
    """원본 PR(#1) 결과 코멘트의 마지막 본문 (제안 PR을 닫는 호출 등은 뺀다)."""
    out = []
    for args, stdin in env["gh"].calls:
        joined = " ".join(args)
        if stdin and ("issues/1/comments" in joined or "issues/comments/" in joined):
            out.append(json.loads(stdin)["body"])
    return out[-1]


def _push(env, sha: str) -> str:
    """원본 PR에 커밋 sha가 올라온 웹훅."""
    env["gh"].head = sha
    env["redis"].set(state_keys.latest_head("o/r", 1), sha)
    job = PRJob(repo="o/r", pr_number=1, kind="update", head_sha=sha, base_sha="b" * 40,
                source="webhook")  # fmt: skip
    return main.handle(job.model_dump_json(), env["deps"])


def _pull(env, skill: str) -> int:
    (number,) = [
        n for n, p in env["gh"].pulls.items()
        if p["head"] == f"pipeline/pr-1-fix-{skill}" and p["state"] == "open"
    ]  # fmt: skip
    return number


def test_each_new_commit_runs_the_auto_path_again(auto_env) -> None:
    assert _push(auto_env, FIRST) == "passed"
    first = _pull(auto_env, "lint")
    assert _push(auto_env, SECOND) == "passed"
    second = _pull(auto_env, "lint")
    assert first != second  # 이전 커밋의 제안은 닫고 새 커밋에서 다시 만들었다
    assert auto_env["gh"].pulls[first]["state"] == "closed"
    assert auto_env["state"]["fix_calls"] == 2
    states = sorted(p.state for p in auto_env["deps"].store.proposals("o/r", 1))
    assert states == ["open", "superseded"]


def test_merge_commit_of_a_proposal_does_not_propose_it_again(auto_env) -> None:
    _push(auto_env, FIRST)
    lint_pr = _pull(auto_env, "lint")
    merge = "d" * 40
    auto_env["gh"].pulls[lint_pr].update(state="closed", merged=True, merge_commit=merge)
    _push(auto_env, merge)
    assert auto_env["state"]["fix_calls"] == 1  # lint 수정을 다시 하지 않았다
    body = _comment(auto_env)
    assert f"lint 제안 PR(#{lint_pr})을 머지한 결과라 같은 제안을 다시 만들지 않습니다" in body
    assert "**lint 수정안:**" not in body


def test_rejected_lint_fix_is_skipped_on_the_next_commit(auto_env) -> None:
    _push(auto_env, FIRST)
    lint_pr = _pull(auto_env, "lint")
    auto_env["gh"].pulls[lint_pr]["state"] = "closed"
    _push(auto_env, SECOND)
    assert auto_env["state"]["fix_calls"] == 1  # 고칠 수 있는 lint 위반이 거절된 것뿐이다
    body = _comment(auto_env)
    assert f"`F401` `sys` imported but unused · 이전에 거절됨 (#{lint_pr})" in body
    assert "**lint 수정안:**" not in body


def test_rejected_lint_line_is_passed_to_the_fixer_as_skip(auto_env) -> None:
    """거절된 줄과 새 위반이 함께 있으면 새 위반만 고친다 (ruff 수정에 skip으로 넘긴다)."""
    _push(auto_env, FIRST)
    auto_env["gh"].pulls[_pull(auto_env, "lint")]["state"] = "closed"
    other = dataclasses.replace(FIXABLE, line=1, message="other", code="F811")
    mp = auto_env["monkeypatch"]
    mp.setattr(
        lint, "run_lint", lambda *a, **k: SkillResult("lint", (FIXABLE, other, UNFIXABLE), "x")
    )
    seen: dict = {}
    real = lint.fix_lint

    def capture(*a, **kw):
        seen.update(kw)
        return real(*a, **kw)

    mp.setattr(lint, "fix_lint", capture)
    _push(auto_env, SECOND)
    assert seen["skip"] == frozenset({("a.py", "F401", "import sys")})
