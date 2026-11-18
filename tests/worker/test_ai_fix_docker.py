"""AI 수정 루프를 실제 ruff(static 재검사)와 실제 샌드박스 pytest로 돌린다. AI만 가짜다.

예제 리포(Eunryong/eval)에서 PR이 `MAX_COUPON_PERCENT` 대신 매직 값 50을 쓴 상황을 만들고,
가짜 AI가 첫 회차에 동작을 바꾸는 상수(40)를 넣었다가 테스트 실패를 받고 두 번째 회차에 고친다.
"""

import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Final

import pytest

from app.models.repo_config import RepoConfig
from app.worker import coverage, graph, repo
from app.worker.ai.claude_cli import ClaudeResult
from app.worker.ai_fix import CodeFixTools, code_fix
from app.worker.base_config import BaseConfig
from app.worker.skills import static
from tests.sandbox_names import TEST_NAMES

pytestmark = pytest.mark.docker

BASE: Final = BaseConfig(
    repo=RepoConfig(), memory_gb=1, max_memory_gb=4, ruff_config=None, ruff_source="기본 규칙"
)
PRICING: Final = "orders/services/pricing.py"
ORIGINAL: Final = "order.coupon_percent > MAX_COUPON_PERCENT"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip


def _commit(wt: Path, message: str) -> str:
    _git(wt, "add", ".")
    _git(wt, "commit", "-q", "-m", message)
    return _git(wt, "rev-parse", "HEAD").strip()


def _ok() -> ClaudeResult:
    return ClaudeResult(ok=True, error=None, cost_usd=0.01, num_turns=1, permission_denials=(),
                        text="50을 상수로 바꿨습니다")  # fmt: skip


def test_ai_fix_loop_with_real_static_and_sandbox(worktree: Path, venv_cache: Path) -> None:
    wt = worktree
    _git(wt, "init", "-q", "-b", "main")
    repo.record_git_dir(wt)
    base_sha = _commit(wt, "base")
    pricing = wt / PRICING
    original = pricing.read_text()
    pricing.write_text(original.replace(ORIGINAL, "order.coupon_percent > 50"))
    head_sha = _commit(wt, "pr: 매직 값")
    repo.write_changes_diff(wt, base_sha, head_sha)

    before = static.run_static(wt, BASE, names=TEST_NAMES)
    assert before is not None and [(f.file, f.code) for f in before.findings] == [
        (PRICING, "PLR2004")
    ]

    def tests() -> object:
        return graph.run_pr_tests(wt, venv_cache=venv_cache, names=TEST_NAMES)

    test_before = tests()
    assert test_before.status == "passed"
    marked = coverage.annotate_coverage(before.findings, test_before.coverage_xml, wt)
    assert [f.covered for f in marked] == [True]  # 이 줄은 테스트가 지나간다
    before = replace(before, findings=marked)

    def wrong(w: Path) -> ClaudeResult:  # 매직 값은 없어지지만 동작이 바뀐다
        text = original.replace(ORIGINAL, "order.coupon_percent > COUPON_CAP")
        (w / PRICING).write_text(
            text.replace("MAX_COUPON_PERCENT = 50", "MAX_COUPON_PERCENT = 50\nCOUPON_CAP = 40")
        )
        return _ok()

    def right(w: Path) -> ClaudeResult:
        (w / PRICING).write_text(original)
        return _ok()

    actions = [wrong, right]
    prompts: list[str] = []

    def run_ai(worktree, prompt, *, model, max_budget_usd):
        prompts.append(prompt)
        return actions.pop(0)(worktree)

    tools = CodeFixTools(
        run_ai=run_ai,
        recheck=lambda changed: static.run_static(wt, BASE, names=TEST_NAMES, changed=changed),
        recheck_lint=None,
        run_tests=tests,
    )
    out = code_fix(
        wt, base_sha, "static", before, list(marked), lint_before=None, test_before=test_before,
        config=RepoConfig(), spent_usd=0.0, tools=tools,
    )  # fmt: skip
    assert out is not None and out.status == "ready", out
    assert out.ai_calls == 2 and out.changed_files == (PRICING,)
    assert "고친 뒤 테스트: 실패" in prompts[1]  # 첫 회차의 테스트 실패가 다음 지시에 들어갔다
    assert out.test_run.status == "passed"
    assert repo.head_diff(wt).count("\n+") >= 1
