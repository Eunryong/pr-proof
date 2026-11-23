"""app/worker/rules/judge.py: 물을 파일 고르기, 저장한 답을 다시 쓸지 정하기(4-2), 다시 물을 때의
지난 판단, 호출 상한. 모델의 답에 기대지 않는다: 부르면 프롬프트만 남기고 실패로 끝나는 호출을
쓴다."""

import re
import subprocess
from pathlib import Path
from typing import Final

import pytest

from app.worker import repo
from app.worker.ai import workdir
from app.worker.ai.claude_backend import ClaudeCliBackend
from app.worker.ai.claude_cli import AIErrorKind, StructuredResult
from app.worker.budget import CallBudget
from app.worker.diff_pure import added_lines
from app.worker.repo_config_pure import CallSettings
from app.worker.rule_pure import CheckMethod, Rule, RuleSource
from app.worker.rules.detect import source_lines
from app.worker.rules.judge import Judged, judge
from app.worker.rules.judge_pure import (
    PROMPT_VERSION,
    Origin,
    flags_to_json,
    hunks_for,
    judge_key,
    origin_from_json,
)
from app.worker.state import Store

from ...redis_stand_ins import MemoryKeys

HEAD: Final = "a" * 40
NAMES: Final = Rule(
    id="R-readme-names", text="함수 이름은 하는 일을 동사로 시작한다.",
    source=RuleSource("README.md", "코드 규칙 > 이름", "b"), origin="document", scope=(),
    check=CheckMethod("checker"), fix="model",
)  # fmt: skip
MONEY: Final = Rule(
    id="R-readme-money", text="금액은 원 단위 정수로 다룬다.",
    source=RuleSource("README.md", "코드 규칙 > 금액", "b"), origin="document",
    scope=("orders/**",), check=CheckMethod("checker"), fix="model",
)  # fmt: skip
PRICING: Final = "def line_amount(item):\n    return item.price * item.quantity\n"
HANDLER: Final = "def body(order):\n    return {}\n"


def _diff(file: str, text: str) -> str:
    lines = text.splitlines()
    return (f"diff --git a/{file} b/{file}\nnew file mode 100644\n--- /dev/null\n+++ b/{file}\n"
            f"@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{line}\n" for line in lines))  # fmt: skip


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    root = tmp_path / "wt"
    (root / "orders").mkdir(parents=True)
    (root / "orders/pricing.py").write_text(PRICING)
    (root / "api.py").write_text(HANDLER)
    (root / "README.md").write_text("# x\n")
    # 판단은 worktree의 파일을 모델 폴더로 복사해 읽게 한다 (git ls-files로 고른다)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    repo.record_git_dir(root)
    return root


DIFF: Final = (
    _diff("orders/pricing.py", PRICING) + _diff("api.py", HANDLER) + _diff("README.md", "# x\n")
)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "pipeline.db", MemoryKeys())


class _Asked:
    """부르면 프롬프트를 남기고 실패로 끝나는 호출. 무엇을 물었는지만 본다."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def __call__(self, prompt: str, **kw) -> StructuredResult:
        self.prompts.append(prompt)
        return StructuredResult(ok=False, error=AIErrorKind.TIMEOUT, data=None, model_id="",
                                input_tokens=None, output_tokens=None, cost_usd=None,
                                permission_denials=0)  # fmt: skip

    def files(self) -> list[set[str]]:
        return [set(re.findall(r"^파일: (.+)$", p, re.M)) for p in self.prompts]


def _run(store: Store, ask: _Asked, worktree: Path, *, rules: tuple[Rule, ...] = (NAMES, MONEY),
         calls: CallSettings | None = None) -> Judged:  # fmt: skip
    budget = CallBudget(store, "o/r", 1, HEAD, calls or CallSettings())
    return judge(
        rules,
        worktree,
        DIFF,
        store=store,
        budget=budget,
        model="sonnet",
        read_code=ClaudeCliBackend(run_structured=ask).reading,
    )


def test_daily_cap_stops_judging(store, worktree) -> None:
    asked = _Asked()
    result = _run(store, asked, worktree, calls=CallSettings(repo_daily=0))
    assert asked.prompts == []
    assert {g.kind for g in result.gaps.values()} == {"cap"}
    assert "저장소 하루 상한" in result.gaps[NAMES.id].detail


def test_unreadable_or_non_python_files_are_not_judged(store, worktree) -> None:
    (worktree / "api.py").unlink()
    (worktree / "api.py").symlink_to(worktree / "orders/pricing.py")
    asked = _Asked()
    _run(store, asked, worktree)
    assert asked.files() == [{"orders/pricing.py"}]


def test_no_rules_means_no_call(store, worktree) -> None:
    asked = _Asked()
    assert _run(store, asked, worktree, rules=()).violations == () and asked.prompts == []


def _save_answer(store: Store, worktree: Path, reads: dict[str, str]) -> None:
    """api.py의 hunk에 대한 NAMES의 답(위반 없음)을, 모델이 reads를 읽고 낸 것으로 저장해 둔다."""
    changed = added_lines(_diff_api())["api.py"]
    (hunk,) = hunks_for("api.py", source_lines(worktree, "api.py"), changed)
    store.save_result(judge_key(NAMES, hunk, "sonnet"), stage="judge", model="sonnet",
                      prompt_version=PROMPT_VERSION,
                      result_json=flags_to_json(hunk, [], reads))  # fmt: skip


def _diff_api() -> str:
    return _diff("api.py", HANDLER)


def test_stored_answer_is_reused_only_while_the_files_the_model_read_are_unchanged(
    store: Store, worktree: Path
) -> None:
    """답이 hunk 밖의 코드에도 달려 있으므로, 같은 hunk여도 모델이 읽은 파일이 바뀌면 다시
    묻는다."""
    files = repo.list_files(worktree)
    reads = {p: workdir.path_digest(worktree, p, files) for p in ("orders/pricing.py", "orders")}
    _save_answer(store, worktree, reads)
    asked = _Asked()

    def run() -> Judged:
        budget = CallBudget(store, "o/r", 1, HEAD, CallSettings())
        return judge((NAMES,), worktree, _diff_api(), store=store, budget=budget, model="sonnet",
                     read_code=ClaudeCliBackend(run_structured=asked).reading)  # fmt: skip

    (worktree / "README.md").write_text("# 읽지 않은 파일만 바뀜\n")
    kept = run()
    assert asked.prompts == [] and kept.usage.cached == 1 and kept.gaps == {}
    (worktree / "orders/pricing.py").write_text(PRICING + "RATE = 2\n")
    changed = run()
    assert len(asked.prompts) == 1 and changed.usage.cached == 0
    assert changed.gaps[NAMES.id].kind == "model_failed"


def _commit(worktree: Path, message: str) -> str:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=worktree,
            check=True, capture_output=True, text=True,
        ).stdout.strip()  # fmt: skip

    git("add", "-A")
    git("commit", "-q", "-m", message)
    return git("rev-parse", "HEAD")


def _reask_prompt(store: Store, worktree: Path, *, pr: int = 1, head: str) -> tuple[str, Judged]:
    """읽은 곳이 바뀐 저장된 쌍을 다시 물을 때의 프롬프트. 호출은 실패로 끝난다(답을 쓰지 않음)."""
    asked = _Asked()
    budget = CallBudget(store, "o/r", pr, head, CallSettings())
    result = judge((NAMES,), worktree, _diff_api(), store=store, budget=budget, model="sonnet",
                   read_code=ClaudeCliBackend(run_structured=asked).reading)  # fmt: skip
    (prompt,) = asked.prompts
    return prompt, result


def _save_prior(store: Store, worktree: Path, sha: str, pr: str = "o/r#1") -> str:
    changed = added_lines(_diff_api())["api.py"]
    (hunk,) = hunks_for("api.py", source_lines(worktree, "api.py"), changed)
    files = repo.list_files(worktree)
    reads = {"orders/pricing.py": workdir.path_digest(worktree, "orders/pricing.py", files)}
    store.save_result(
        judge_key(NAMES, hunk, "sonnet"), stage="judge", model="sonnet",
        prompt_version=PROMPT_VERSION,
        result_json=flags_to_json(hunk, [], reads, basis="pricing.py의 line_amount를 읽음",
                                  origin=Origin(pr, sha)),
    )  # fmt: skip
    return judge_key(NAMES, hunk, "sonnet")


def test_reasked_pair_gets_its_prior_answer_and_the_files_changed_since_that_commit(
    store: Store, worktree: Path
) -> None:
    """같은 PR의 앞 커밋에서 낸 답은 지난 판단, 근거, 그 커밋 뒤 바뀐 파일(git)과 함께 다시 묻는다.
    호출이 실패하면 저장한 답은 그대로 남는다."""
    first = _commit(worktree, "c1")
    key = _save_prior(store, worktree, first)
    (worktree / "orders/pricing.py").write_text(PRICING + "RATE = 2\n")
    (worktree / "notes.txt").write_text("메모\n")
    second = _commit(worktree, "c2")
    prompt, result = _reask_prompt(store, worktree, head=second)
    assert f"<<<PRIOR n=1 rule={NAMES.id} tag=" in prompt
    assert "지난 판단: 위반 없음\n근거: pricing.py의 line_amount를 읽음" in prompt
    assert (
        f"커밋 {first[:12]} 뒤 지금 커밋까지 바뀐 파일 2개:\nnotes.txt\norders/pricing.py" in prompt
    )
    assert "그중 지난번에 읽은 곳 안의 파일: orders/pricing.py" in prompt
    assert "지난번에 읽은 곳 중 내용이 달라진 곳: orders/pricing.py" in prompt
    assert result.usage.reasked == 1 and result.usage.kept_prior == 0
    entry = store.cached_result(key)
    assert entry is not None and origin_from_json(entry.result_json).sha == first


def test_prior_answer_from_another_pr_is_not_shown(store: Store, worktree: Path) -> None:
    """저장 키는 PR을 가리지 않는다. 다른 PR이 낸 같은 키의 답은 지난 판단으로 보이지 않는다."""
    first = _commit(worktree, "c1")
    _save_prior(store, worktree, first, pr="o/r#7")
    (worktree / "orders/pricing.py").write_text(PRICING + "RATE = 2\n")
    prompt, result = _reask_prompt(store, worktree, head=_commit(worktree, "c2"))
    assert "<<<PRIOR" not in prompt and result.usage.reasked == 0


def test_prior_commit_that_is_gone_means_changes_are_unknown(store: Store, worktree: Path) -> None:
    _commit(worktree, "c1")
    _save_prior(store, worktree, "f" * 40)  # 저장소에 없는 커밋 (강제 push 뒤 지워짐 등)
    (worktree / "orders/pricing.py").write_text(PRICING + "RATE = 2\n")
    prompt, _ = _reask_prompt(store, worktree, head=_commit(worktree, "c2"))
    assert "그 뒤 바뀐 파일: 알 수 없음" in prompt
    assert "지난번에 읽은 곳 중 내용이 달라진 곳: orders/pricing.py" in prompt
