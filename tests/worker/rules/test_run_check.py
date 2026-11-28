"""app/worker/rules/run_check.py: 프로필이 돌릴 것이 없는 저장소에서는 모델을 부르지 않는다."""

from typing import cast

from app.worker.core.rule_pure import CheckMethod, Rule, RuleSource
from app.worker.rules.doc_rules import Runtime
from app.worker.rules.doc_rules_pure import CallUsage, Found, RuleUnchecked, RunJudged
from app.worker.rules.run_check import judge_runs
from app.worker.testing.test_run_pure import NO_PROFILE_WHY, PytestRun


class _Untouched:
    """모델, 저장소, 호출 상한 어느 것도 쓰이면 안 되는 자리."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"프로필이 없는 저장소에서 {name}을 썼다")


def test_no_profile_answers_without_the_model_the_store_or_the_budget() -> None:
    rule = Rule(
        id="R-build", text="빌드가 경고 없이 성공한다.", source=RuleSource("C.md", "s", "0"),
        origin="document", scope=(), check=CheckMethod("none"), fix="none", target="run",
        runs=("tests",),
    )  # fmt: skip
    found = Found((RuleUnchecked(rule, "run", ""),), (), 0, CallUsage(0, 10, 0, (), 0, 0))
    run = PytestRun(exit_code=None, summary=None, no_profile=True)
    runs = judge_runs(found, run, cast(Runtime, _Untouched()))
    assert runs == (RunJudged("R-build", "left", NO_PROFILE_WHY, ""),)
