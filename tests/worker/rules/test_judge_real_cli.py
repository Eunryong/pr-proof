"""실제 claude CLI로 확인한다: 위반 판단 모델이 hunk 밖의 코드를 찾아 읽고 판단한다 (4.5).

운영에서 "확인하지 못한 규칙"이 나온 모양을 작게 재현한다. PR은
`sample["verified"] = _validate(sample)` 한 줄만 더하고, `_validate`의 본문은 다른 파일에 있다.
본문이 규칙의 검증 함수를 부르면(통과하지 못하면 예외) 위반이 없고, 부르지 않으면 그 줄이
위반이다. 어느 쪽이든 `undecided`로 남으면 안 된다. 같은 입력의 두 번째 판단은 저장한 답을 쓰고,
모델이 읽은 파일이 바뀌면 다시 묻는다.

모델을 실제로 부르므로 `PR_PROOF_REAL_AI=1`일 때만 돈다. 호출은 케이스마다 1–2회다.
"""

import os
import subprocess
from pathlib import Path
from typing import Final

import fakeredis
import pytest

from app.worker import repo
from app.worker.ai import claude_cli
from app.worker.budget import CallBudget
from app.worker.repo_config_pure import CallSettings
from app.worker.rule_pure import CheckMethod, Rule, RuleSource
from app.worker.rules.judge import Judged, judge
from app.worker.state import Store

REAL_AI_ENV: Final = "PR_PROOF_REAL_AI"
MODEL: Final = "sonnet"
RULE: Final = Rule(
    id="R-readme-verified",
    text="생성된 모든 샘플은 `validators.validate_example` 검증을 통과하고 "
    '`"verified": true` 필드를 포함해야 한다.',
    source=RuleSource("README.md", "데이터 규칙", "b"), origin="document", scope=(),
    check=CheckMethod("model"), fix="model",
)  # fmt: skip
VALIDATORS: Final = (
    "def validate_example(sample):\n"
    '    """질문과 답이 있고 답이 숫자인지 본다."""\n'
    '    return bool(sample.get("question")) and str(sample.get("answer", "")).isdigit()\n'
)
# 통과하지 못하면 예외라 verified에는 늘 True가 들어간다
CALLS_VALIDATOR: Final = (
    "from validators import validate_example\n\n\n"
    "def _validate(sample):\n"
    "    if not validate_example(sample):\n"
    '        raise ValueError("검증을 통과하지 못한 샘플")\n'
    "    return True\n"
)
SKIPS_VALIDATOR: Final = (
    "def _validate(sample):\n"
    '    if "answer" not in sample:\n'
    '        raise ValueError("답이 없는 샘플")\n'
    "    return True\n"
)
MAKE_BASE: Final = (
    "from gen.checks import _validate\n\n\n"
    "def make_sample(question, answer):\n"
    '    sample = {"question": question, "answer": answer}\n'
    "    return sample\n"
)
ADDED: Final = '    sample["verified"] = _validate(sample)'
MAKE_HEAD: Final = MAKE_BASE.replace("    return sample\n", f"{ADDED}\n    return sample\n")

pytestmark = pytest.mark.skipif(
    os.environ.get(REAL_AI_ENV) != "1", reason=f"{REAL_AI_ENV}=1일 때만 실제 모델을 부른다"
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip


def _worktree(tmp_path: Path, checks: str) -> tuple[Path, str]:
    """(worktree, PR의 diff). base는 `make.py`에 그 줄이 없고 head가 그 줄을 더한다."""
    wt = tmp_path / "wt"
    (wt / "gen").mkdir(parents=True)
    _git(wt, "init", "-q", "-b", "main")
    repo.record_git_dir(wt)
    (wt / "validators.py").write_text(VALIDATORS)
    (wt / "gen" / "__init__.py").write_text("")
    (wt / "gen" / "checks.py").write_text(checks)
    (wt / "gen" / "make.py").write_text(MAKE_BASE)
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "base")
    (wt / "gen" / "make.py").write_text(MAKE_HEAD)
    _git(wt, "commit", "-qam", "head")
    return wt, _git(wt, "diff", "HEAD~1", "HEAD")


def _judge(wt: Path, diff: str, store: Store) -> Judged:
    budget = CallBudget(store, "o/r", 1, "a" * 40, CallSettings())
    return judge((RULE,), wt, diff, store=store, budget=budget, model=MODEL,
                 ask=claude_cli.run_structured)  # fmt: skip


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "pipeline.db", fakeredis.FakeStrictRedis())


def test_body_that_calls_the_validator_is_read_and_kept(tmp_path: Path, store: Store) -> None:
    """(가) `_validate`가 `validate_example`을 부른다 → 위반 없음. 같은 입력은 저장한 답을 쓴다."""
    wt, diff = _worktree(tmp_path, CALLS_VALIDATOR)
    first = _judge(wt, diff, store)
    assert first.usage.calls == 1 and first.gaps == {}, first
    assert first.undecided == {} and first.violations == (), first
    again = _judge(wt, diff, store)
    assert again.usage.calls == 0 and again.usage.cached == 1 and again.violations == ()


def test_body_that_skips_the_validator_is_read_and_flagged(tmp_path: Path, store: Store) -> None:
    """(나) `_validate`가 검증 함수를 부르지 않는다 → 그 줄이 위반. 읽은 파일이 바뀌면 다시
    묻는다."""
    wt, diff = _worktree(tmp_path, SKIPS_VALIDATOR)
    first = _judge(wt, diff, store)
    assert first.usage.calls == 1 and first.gaps == {} and first.undecided == {}, first
    (v,) = first.violations
    assert (v.file, v.line_text, v.found_by) == ("gen/make.py", ADDED.strip(), "model")
    (wt / "gen" / "checks.py").write_text(CALLS_VALIDATOR)  # hunk는 그대로, 읽은 파일만 바뀜
    second = _judge(wt, diff, store)
    assert second.usage.calls == 1 and second.usage.cached == 0, second
    assert second.violations == () and second.undecided == {}, second
