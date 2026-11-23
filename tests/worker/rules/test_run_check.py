"""app/worker/rules/run_check.py: 실행 결과의 규칙의 저장과 상한 (4.5, 9.2). 모델의 답은 직접 만든
값을 넣는다. 모델이 그 답을 내는지는 실제 모델로 본다(DESIGN 11.1 2026-11-22)."""

from pathlib import Path
from typing import Final

import pytest

from app.worker.ai.claude_backend import ClaudeCliBackend
from app.worker.ai.claude_cli import AIErrorKind, StructuredResult
from app.worker.config.repo_config_pure import CallSettings, ModelSettings
from app.worker.core.rule_pure import CheckMethod, Rule, RuleSource
from app.worker.doc_rules_pure import CallUsage, Found, RuleUnchecked
from app.worker.rules.doc_rules import Runtime
from app.worker.rules.run_check import judge_runs
from app.worker.store.budget import CallBudget
from app.worker.store.state import Store
from app.worker.testing.test_run_pure import PytestRun, ReportSummary

from ...redis_stand_ins import MemoryKeys

RULE: Final = Rule(
    id="R-readme-run", text="데이터 생성기 테스트 통과", source=RuleSource("README.md", "s", "b"),
    origin="document", scope=(), check=CheckMethod("none"), fix="none", target="run",
    runs=("tests",),
)  # fmt: skip
FOUND: Final = Found((RuleUnchecked(RULE, "run", ""),), (), 0, CallUsage(0, 10, 0, (), 0, 0))
RUN: Final = PytestRun(exit_code=0, summary=ReportSummary(passed=6, total=6))
DIFF: Final = (
    b"diff --git a/gen.py b/gen.py\n--- /dev/null\n+++ b/gen.py\n@@ -0,0 +1,1 @@\n+x = 1\n"
)


def _answer(verdict: str = "broken", ok: bool = True) -> StructuredResult:
    return StructuredResult(
        ok=ok, error=None if ok else AIErrorKind.TIMEOUT, model_id="claude-sonnet-5",
        input_tokens=10, output_tokens=5, cost_usd=0.0, permission_denials=0,
        data={"verdict": verdict, "why": "생성기를 실행하는 테스트가 없음", "files": ["gen.py"]},
        detail="" if ok else "느림",
    )  # fmt: skip


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "pipeline.db", MemoryKeys())


def _rt(
    store: Store, answers: list[StructuredResult], calls: CallSettings | None = None
) -> Runtime:
    def ask(prompt: str, **kw) -> StructuredResult:
        return answers.pop(0)

    budget = CallBudget(store, "o/r", 1, "a" * 40, calls or CallSettings())
    return Runtime(store=store, budget=budget, models=ModelSettings(),
                   backend=ClaudeCliBackend(run_structured=ask), sandbox=None)  # fmt: skip


def test_answer_is_stored_by_the_results_and_reused(store: Store) -> None:
    (first,) = judge_runs(FOUND, RUN, DIFF, _rt(store, [_answer()]))
    assert (first.rule_id, first.verdict) == ("R-readme-run", "broken")
    assert first.why == "생성기를 실행하는 테스트가 없음 (테스트가 필요한 파일: gen.py)"
    assert "테스트 통과 (6개)" in first.facts
    (again,) = judge_runs(FOUND, RUN, DIFF, _rt(store, []))
    assert again == first
    assert [c.stage for c in store.model_calls("o/r", 1) if not c.cache_hit] == ["run_judge"]


def test_cap_and_failure_leave_the_rule_unanswered_and_unsaved(store: Store) -> None:
    capped = _rt(store, [], CallSettings(pr_base=0, pr_max=0))
    (left,) = judge_runs(FOUND, RUN, DIFF, capped)
    assert left.verdict == "left" and "PR 상한" in left.why
    (failed,) = judge_runs(FOUND, RUN, DIFF, _rt(store, [_answer(ok=False)]))
    assert failed.verdict == "left"
    (later,) = judge_runs(FOUND, RUN, DIFF, _rt(store, [_answer("kept")]))
    assert later.verdict == "kept"


def test_no_run_rules_means_no_call(store: Store) -> None:
    empty = Found((), (), 0, CallUsage(0, 10, 0, (), 0, 0))
    assert judge_runs(empty, RUN, DIFF, _rt(store, [])) == ()
