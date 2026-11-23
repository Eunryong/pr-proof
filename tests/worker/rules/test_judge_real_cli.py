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
from app.worker.ai.claude_backend import ClaudeCliBackend
from app.worker.budget import CallBudget
from app.worker.diff_pure import added_lines
from app.worker.repo_config_pure import CallSettings
from app.worker.rule_pure import CheckMethod, Rule, RuleSource
from app.worker.rules.detect import source_lines
from app.worker.rules.judge import Judged, judge
from app.worker.rules.judge_pure import basis_from_json, hunks_for, judge_key, reads_from_json
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
                 read_code=ClaudeCliBackend().reading)  # fmt: skip


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


class _Recorder:
    """실제 호출을 그대로 하고 도구 횟수와 토큰을 적어 둔다."""

    def __init__(self) -> None:
        self.results: list[claude_cli.StructuredResult] = []

    def __call__(self, prompt: str, **kw: object) -> claude_cli.StructuredResult:
        result = claude_cli.run_structured(prompt, **kw)  # type: ignore[arg-type]
        self.results.append(result)
        return result

    def since(self, start: int) -> tuple[int, int, int, int]:
        """start번째 뒤의 (호출, 도구, 입력 토큰, 출력 토큰)."""
        rs = self.results[start:]
        return (len(rs), sum(r.tool_uses for r in rs), sum(r.input_tokens or 0 for r in rs),
                sum(r.output_tokens or 0 for r in rs))  # fmt: skip


def _stored(wt: Path, diff: str, store: Store) -> str:
    changed = added_lines(diff)["gen/make.py"]
    (hunk,) = hunks_for("gen/make.py", source_lines(wt, "gen/make.py"), changed)
    entry = store.cached_result(judge_key(RULE, hunk, MODEL))
    assert entry is not None
    return entry.result_json


def test_prior_answer_is_kept_for_unrelated_changes_and_revised_when_the_body_changes(
    tmp_path: Path, store: Store
) -> None:
    """(가) 첫 판단은 위반 없음과 근거 메모를 저장한다. (나) 관계없는 변경만 있는 다음 커밋은 다시
    묻더라도 지난 판단을 유지한다(글이 같다). 모델이 읽은 곳 밖만 바뀌어 다시 묻지 않았으면, 읽은
    파일에 동작과 관계없는 주석을 더해 다시 묻게 한다. (다) `_validate`가 검증 함수를 부르지 않게
    바뀐 커밋은 위반으로 바뀌고 근거에 까닭이 적힌다. 호출은 3–4회다."""
    wt, diff = _worktree(tmp_path, CALLS_VALIDATOR)
    rec = _Recorder()

    def judge_at_head() -> Judged:
        head = _git(wt, "rev-parse", "HEAD").strip()
        budget = CallBudget(store, "o/r", 1, head, CallSettings())
        return judge((RULE,), wt, diff, store=store, budget=budget, model=MODEL,
                     read_code=ClaudeCliBackend(run_structured=rec).reading)  # fmt: skip

    first = judge_at_head()
    stored = _stored(wt, diff, store)
    print("\n(가)", rec.since(0), basis_from_json(stored), reads_from_json(stored).keys())
    assert first.violations == () and first.undecided == {} and first.gaps == {}, first
    assert basis_from_json(stored), stored

    (wt / "notes.txt").write_text("메모\n")
    (wt / "other.py").write_text("def unrelated():\n    return 1\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "관계없는 파일")
    start = len(rec.results)
    second = judge_at_head()
    print("(나)", rec.since(start), second.usage)
    if second.usage.calls == 0:  # 모델이 읽은 곳 밖만 바뀌었다
        assert second.usage.cached == 1 and second.violations == ()
        (wt / "gen" / "checks.py").write_text("# 샘플 검증\n" + CALLS_VALIDATOR)
        _git(wt, "commit", "-qam", "주석만 더함")
        start = len(rec.results)
        second = judge_at_head()
        print("(나')", rec.since(start), second.usage)
    kept = _stored(wt, diff, store)
    print("(나) 근거", basis_from_json(kept))
    assert second.usage.reasked == 1 and second.usage.kept_prior == 1, second
    assert second.violations == () and second.undecided == {}, second
    assert basis_from_json(kept) == basis_from_json(stored)

    (wt / "gen" / "checks.py").write_text(SKIPS_VALIDATOR)
    _git(wt, "commit", "-qam", "검증 함수를 부르지 않음")
    start = len(rec.results)
    third = judge_at_head()
    changed = _stored(wt, diff, store)
    print("(다)", rec.since(start), third.usage, basis_from_json(changed))
    assert third.usage.reasked == 1 and third.usage.kept_prior == 0, third
    (v,) = third.violations
    assert (v.file, v.line_text) == ("gen/make.py", ADDED.strip()), v
    assert basis_from_json(changed) and basis_from_json(changed) != basis_from_json(stored)
