"""app/worker/rules/pr_check.py: PR 단위 확인의 계획(저장, 회차, 모델 판단으로 보내기), PR 패턴,
바뀐 파일 조건, 모델 판단(저장, 상한, 실패), 사실을 읽지 못함 (4.5 끝, 4-11, 4-12).

샌드박스는 고정 스크립트(app/worker/sandbox/checker_runner.py의 PR 패턴)를 이 프로세스에서 그대로
돌리는 대역이다. 실제 컨테이너는 tests/worker/sandbox의 docker 테스트가 본다.
"""

import contextlib
import io
import json
import re
import sys
from typing import Final
from unittest import mock

import pytest

from app.worker.config.repo_config_pure import CallSettings
from app.worker.core.rule_pure import CheckMethod, Example, Rule, RuleSource
from app.worker.model.claude.claude_cli import AIErrorKind, StructuredResult
from app.worker.rules import pr_check, pr_check_pure
from app.worker.rules.pr_check_pure import Commit, PrFacts
from app.worker.sandbox import checker_runner
from app.worker.sandbox.checker_runner import Failed, PrRan
from app.worker.sandbox.toolcache import ToolUnavailable
from app.worker.store.budget import CallBudget
from app.worker.store.call_budget_pure import RulesetScope
from app.worker.store.state import Store

from ...redis_stand_ins import MemoryKeys

HEAD: Final = "f" * 40
SHA_A: Final = "a" * 40
SHA_B: Final = "b" * 40
SHA_C: Final = "c" * 40


def _rule(rid: str, text: str, examples=()) -> Rule:
    return Rule(id=rid, text=text, source=RuleSource("README.md", "기여", "0"), origin="document",
                scope=(), check=CheckMethod("none"), fix="none", target="pr",
                examples=tuple(examples))  # fmt: skip


COMMITS: Final = _rule("R-readme-commit", "커밋 메시지는 feat: 나 fix: 로 시작한다.")
MIGRATION: Final = _rule(
    "R-readme-migration", "migrations/ 아래 파일을 바꾸면 PR 본문에 ## 마이그레이션 절을 둔다."
)
TESTING: Final = _rule("R-readme-testing", "PR 본문에 테스트 방법을 적는다.")
BRANCH: Final = _rule("R-readme-branch", "브랜치 이름은 feat/ 로 시작한다.")


def _ok(data: dict, tokens: int = 100) -> StructuredResult:
    return StructuredResult(ok=True, error=None, data=data, model_id="claude-sonnet-5",
                            input_tokens=tokens, output_tokens=10, cost_usd=0.0,
                            permission_denials=0)  # fmt: skip


COMMIT_PLAN: Final = {
    "method": "pattern", "fact": "commit_subject", "condition": "must_match",
    "pattern": "^(feat|fix): ", "when_changed": "", "reason": "",
    "examples": [
        {"value": "feat: 로그인", "changed_paths": ["a.py"], "violates": False},
        {"value": "fix: 합계\n\n본문", "changed_paths": ["a.py"], "violates": False},
        {"value": "update", "changed_paths": ["a.py"], "violates": True},
        {"value": "WIP feat: x", "changed_paths": ["a.py"], "violates": True},
    ],
}  # fmt: skip
MIGRATION_PLAN: Final = {
    "method": "pattern", "fact": "pr_body", "condition": "must_match",
    "pattern": "(?m)^## 마이그레이션", "when_changed": "^migrations/", "reason": "",
    "examples": [
        {"value": "변경", "changed_paths": ["migrations/0002.py"], "violates": True},
        {"value": "", "changed_paths": ["migrations/0003.py", "a.py"], "violates": True},
        {"value": "## 마이그레이션\n0002", "changed_paths": ["migrations/0002.py"],
         "violates": False},
        {"value": "변경", "changed_paths": ["orders/a.py"], "violates": False},
    ],
}  # fmt: skip
TESTING_PLAN: Final = {
    "method": "model", "fact": "pr_body", "condition": "must_match", "pattern": "",
    "when_changed": "", "reason": "뜻을 읽어야 함", "examples": [],
}  # fmt: skip


def _agree(prompt: str) -> list[dict]:
    """보인 것을 모두 위반이라고 답한다: PR 패턴의 후보를 모델이 확인하는 경우 (4.5 끝)."""
    found = [{"where": "commit", "sha": sha, "quote": "", "why": "후보 확인"}
             for sha in re.findall(r"^<<<COMMIT sha=(\w+) ", prompt, re.M)]  # fmt: skip
    if "<<<PR_BODY " in prompt and not found:
        found.append({"where": "pr_body", "sha": "", "quote": "", "why": "후보 확인"})
    return found


class Model:
    """plans: 규칙 id → 계획 답 목록(회차마다 하나, 모자라면 마지막). judge: 판단의 답. judge가
    None이면 보인 것을 모두 위반이라고 답한다(패턴의 후보를 확인할 때)."""

    def __init__(self, plans: dict[str, list[dict]], judge: list[dict] | None = None,
                 fail: str = "") -> None:  # fmt: skip
        self.plans, self.judge, self.fail = plans, judge, fail
        self.calls: list[dict] = []

    def __call__(self, prompt: str, *, schema, **kw) -> StructuredResult:
        stage = "plan" if schema is pr_check_pure.PLAN_SCHEMA else "judge"
        self.calls.append({"stage": stage, "prompt": prompt, "schema": schema, **kw})
        if self.fail == stage:
            return StructuredResult(ok=False, error=AIErrorKind.TIMEOUT, data=None, model_id="",
                                    input_tokens=None, output_tokens=None, cost_usd=None,
                                    permission_denials=0, detail="느림")  # fmt: skip
        if stage == "judge":
            return _ok({"violations": _agree(prompt) if self.judge is None else self.judge})
        (rule_id,) = [r for r in self.plans if f"id={r} " in prompt]
        answers = self.plans[rule_id]
        done = sum(
            1 for c in self.calls if c["stage"] == "plan" and f"id={rule_id} " in c["prompt"]
        )
        return _ok(answers[min(done, len(answers)) - 1])

    def stages(self) -> list[str]:
        return [c["stage"] for c in self.calls]


class ScriptSandbox:
    """고정 스크립트를 이 프로세스에서 돌린다. fail을 주면 그 결과를 낸다."""

    def __init__(self, fail: object = None) -> None:
        self.fail = fail
        self.runs: list[tuple[dict, list]] = []

    def pr_pattern(self, decl, cases):
        self.runs.append((dict(decl), [dict(c) for c in cases]))
        if isinstance(self.fail, Exception):
            raise self.fail
        if self.fail is not None:
            return self.fail
        out = io.StringIO()
        stdin = io.StringIO(json.dumps({"decl": dict(decl), "cases": [dict(c) for c in cases]}))
        with mock.patch.object(sys, "stdin", stdin), contextlib.redirect_stdout(out):
            exec(checker_runner._PR_SCRIPT, {})  # noqa: S102
        return PrRan(json.loads(out.getvalue()))


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "pipeline.db", MemoryKeys())


def _tools(store: Store, model: Model, sandbox=None, calls: CallSettings | None = None):
    budget = CallBudget(store, "o/r", 1, HEAD, calls or CallSettings())
    return pr_check.Tools(store=store, budget=budget, ask=model, sandbox=sandbox or ScriptSandbox(),
                          plan_model="sonnet", judge_model="sonnet",
                          ruleset=RulesetScope("rules-key", 4))  # fmt: skip


def _facts(*, body: str = "", paths=("orders/a.py",), commits=None, title: str = "제목") -> PrFacts:
    commits = commits if commits is not None else (
        Commit(SHA_A, "feat: 로그인"), Commit(SHA_B, "update readme"), Commit(SHA_C, "fix: 합계"),
    )  # fmt: skip
    return PrFacts(
        title=title, body=body, branch="topic", commits=tuple(commits), paths=tuple(paths)
    )


def test_commit_format_flags_only_the_breaking_commits_by_full_sha(store) -> None:
    """4-11: 형식을 어긴 커밋만 잡힌다. merge 커밋은 보지 않는다."""
    merge = Commit("d" * 40, "Merge branch 'main'", merge=True)
    commits = (Commit(SHA_A, "feat: 로그인"), Commit(SHA_B, "update readme"), merge,
               Commit(SHA_C, "WIP"))  # fmt: skip
    model = Model({COMMITS.id: [COMMIT_PLAN]})
    result = pr_check.check([COMMITS], _facts(commits=commits), _tools(store, model))
    assert [(v.where, v.sha, v.quote) for v in result.violations] == [
        ("commit", SHA_B, "update readme"),
        ("commit", SHA_C, "WIP"),
    ]
    assert result.commits == 3 and result.skipped_merge == 1
    (r,) = result.results
    assert (r.status, r.method, r.decl.pattern) == ("checked", "pattern", "^(feat|fix): ")
    # 패턴에 맞지 않은 커밋은 후보이고, 모델이 그 커밋만 보고 위반인지 정한다 (2026-11-22)
    assert model.stages() == ["plan", "judge"] and result.usage.plan_calls == 1
    assert SHA_A not in model.calls[1]["prompt"] and SHA_B in model.calls[1]["prompt"]
    assert [c.stage for c in store.model_calls("o/r", 1)] == ["pr_plan", "pr_judge"]
    assert store.model_calls("o/r", 1)[0].ruleset_key == "rules-key"


def test_pattern_candidates_the_model_rejects_are_set_aside(store) -> None:
    """패턴이 잡은 것은 후보다. 모델이 위반이라고 답하지 않은 후보는 위반이 아니고 따로 남는다.
    모델은 후보가 아닌 커밋을 위반으로 더할 수 없다."""
    commits = (Commit(SHA_A, "feat(abacus): NoPE"), Commit(SHA_B, "update readme"))
    model = Model(
        {COMMITS.id: [COMMIT_PLAN]},
        judge=[
            {"where": "commit", "sha": SHA_B, "quote": "", "why": "형식이 없음"},
            {"where": "commit", "sha": SHA_C, "quote": "", "why": "후보 아님"},
        ],
    )
    result = pr_check.check([COMMITS], _facts(commits=commits), _tools(store, model))
    assert [(v.sha, v.why) for v in result.violations] == [(SHA_B, "형식이 없음")]
    (r,) = result.results
    assert [v.sha for v in r.dismissed] == [SHA_A]
    capped = Model({COMMITS.id: [COMMIT_PLAN]})
    tools = _tools(store, capped, calls=CallSettings(pr_base=0, pr_max=0))
    (left,) = pr_check.check([COMMITS], _facts(commits=(Commit(SHA_C, "WIP"),)), tools).results
    assert (left.status, left.kind) == ("left", "cap")


def test_plan_is_stored_by_the_rule_and_reused_for_any_pr_text(store) -> None:
    pr_check.check([COMMITS], _facts(), _tools(store, Model({COMMITS.id: [COMMIT_PLAN]})))
    again = Model({})
    other = _facts(body="다른 본문", commits=(Commit(SHA_A, "update"),), title="다른 제목")
    result = pr_check.check([COMMITS], other, _tools(store, again))
    assert again.stages() == ["judge"] and result.usage.cached == 1 and len(result.violations) == 1


def test_plan_prompt_never_contains_pr_text(store) -> None:
    model = Model({COMMITS.id: [COMMIT_PLAN]})
    facts = _facts(body="본문의-지시문", title="제목의-지시문",
                   commits=(Commit(SHA_A, "커밋의-지시문"),))  # fmt: skip
    pr_check.check([COMMITS], facts, _tools(store, model))
    call = model.calls[0]
    assert call["schema"] is pr_check_pure.PLAN_SCHEMA
    assert not any(s in call["prompt"] for s in ("본문의-지시문", "제목의-지시문", "커밋의-지시문"))
    assert call["system_prompt"] == pr_check_pure.PLAN_SYSTEM_PROMPT


@pytest.mark.parametrize(
    "body,paths,flagged,status",
    [
        ("설명\n## 마이그레이션\n0002를 더함", ("migrations/0002.py",), False, "checked"),
        ("설명만 있음", ("migrations/0002.py", "orders/a.py"), True, "checked"),
        ("설명만 있음", ("orders/a.py",), False, "not_applicable"),
        ("", (), False, "not_applicable"),
    ],
)
def test_body_rule_flags_only_prs_that_changed_the_files_and_lack_the_text(
    store, body: str, paths: tuple[str, ...], flagged: bool, status: str
) -> None:
    """4-12: 특정 파일을 바꾼 PR에서 본문이 빠진 PR만 잡힌다. 파일이 바뀌었는지는 파이프라인이
    경로로 정한다."""
    model = Model({MIGRATION.id: [MIGRATION_PLAN]})
    result = pr_check.check([MIGRATION], _facts(body=body, paths=paths), _tools(store, model))
    assert bool(result.violations) is flagged
    assert result.results[0].status == status
    if flagged:
        (v,) = result.violations
        assert (v.where, v.quote, v.found_by) == ("pr_body", "", "pattern")


def test_pattern_failing_examples_twice_goes_to_judgment_with_its_gate(store) -> None:
    broken = {**MIGRATION_PLAN, "pattern": "절대 맞지 않는 패턴"}
    missing = {"where": "pr_body", "sha": "", "quote": "", "why": "절이 없음"}
    model = Model({MIGRATION.id: [broken, broken]}, judge=[missing])
    result = pr_check.check([MIGRATION], _facts(body="x", paths=("migrations/1.py",)),
                            _tools(store, model))  # fmt: skip
    (r,) = result.results
    assert (r.method, r.route, r.decl.when_changed, r.decl.pattern) == (
        "model",
        "examples",
        "^migrations/",
        "",
    )
    assert model.stages() == ["plan", "plan", "judge"]
    assert (
        "예시 3(지키는 예): 선언이 잡음" in model.calls[1]["prompt"]
    )  # 두 번째 회차는 실패를 받는다
    (v,) = result.violations
    assert (v.found_by, v.why) == ("model", "절이 없음")
    # 조건이 맞지 않는 PR은 모델을 부르지 않는다
    quiet = Model({})
    off = pr_check.check([MIGRATION], _facts(body="x", paths=("a.py",)), _tools(store, quiet))
    assert quiet.calls == [] and off.results[0].status == "not_applicable"


def test_branch_pattern_failing_twice_is_unchecked_and_stored(store) -> None:
    bad = {"method": "pattern", "fact": "branch", "condition": "must_match", "pattern": "^x",
           "when_changed": "", "reason": "", "examples": [
               {"value": "feat/a", "changed_paths": [], "violates": False},
               {"value": "feat/b", "changed_paths": [], "violates": False},
               {"value": "main", "changed_paths": [], "violates": True},
               {"value": "topic", "changed_paths": [], "violates": True}]}  # fmt: skip
    model = Model({BRANCH.id: [bad]})
    (r,) = pr_check.check([BRANCH], _facts(), _tools(store, model)).results
    assert (r.status, r.kind) == ("unchecked", "plan_none")
    assert "예시를 통과하는 PR 패턴을 만들지 못함" in r.detail
    again = Model({})
    pr_check.check([BRANCH], _facts(), _tools(store, again))
    assert again.calls == []


def test_format_errors_do_not_count_as_example_failures_but_use_attempts(store) -> None:
    model = Model({COMMITS.id: [{"method": "pattern"}, COMMIT_PLAN]})
    result = pr_check.check([COMMITS], _facts(), _tools(store, model))
    assert result.results[0].method == "pattern" and "답의 형식이 틀림" in model.calls[1]["prompt"]


def test_rule_the_model_says_cannot_be_checked_is_listed_and_stored(store) -> None:
    none = {**TESTING_PLAN, "method": "none", "reason": "실행 결과가 필요함"}
    model = Model({TESTING.id: [none]})
    (r,) = pr_check.check([TESTING], _facts(), _tools(store, model)).results
    assert (r.status, r.kind, r.detail) == ("unchecked", "plan_none", "실행 결과가 필요함")
    assert pr_check.check([TESTING], _facts(), _tools(store, Model({}))).usage.plan_calls == 0


def test_plan_cap_and_model_failure_are_not_stored(store) -> None:
    capped = CallSettings(ruleset_base=0, per_rule=0)
    (r,) = pr_check.check([COMMITS], _facts(), _tools(store, Model({}), calls=capped)).results
    assert (r.status, r.kind) == ("unchecked", "cap") and "규칙 묶음 상한" in r.detail
    failing = Model({COMMITS.id: [COMMIT_PLAN]}, fail="plan")
    (r,) = pr_check.check([COMMITS], _facts(), _tools(store, failing)).results
    assert (r.status, r.kind) == ("unchecked", "model_failed")
    later = Model({COMMITS.id: [COMMIT_PLAN]})
    assert pr_check.check([COMMITS], _facts(), _tools(store, later)).results[0].method == "pattern"
    assert later.stages() == ["plan", "judge"]


def test_judged_rule_answers_are_verified_and_stored(store) -> None:
    answers = [{"where": "pr_body", "sha": "", "quote": "", "why": "테스트 방법이 없음"},
               {"where": "commit", "sha": SHA_A, "quote": "x", "why": "묻지 않은 곳"},
               {"where": "pr_title", "sha": "", "quote": "지어낸 제목",
                "why": "없는 인용"}]  # fmt: skip
    model = Model({TESTING.id: [TESTING_PLAN]}, judge=answers)
    facts = _facts(body="고쳤습니다", title="제목")
    result = pr_check.check([TESTING], facts, _tools(store, model))
    (v,) = result.violations
    assert (v.where, v.found_by, v.why) == ("pr_body", "model", "테스트 방법이 없음")
    assert result.usage.dropped == 2 and result.usage.judge_calls == 1
    judge_call = model.calls[-1]
    assert (
        judge_call["system_prompt"] == pr_check_pure.JUDGE_SYSTEM_PROMPT
        and "고쳤습니다" in judge_call["prompt"]
    )
    assert SHA_A not in judge_call["prompt"]  # 본문 규칙에는 커밋을 넣지 않는다
    again = Model({})
    second = pr_check.check([TESTING], facts, _tools(store, again))
    assert again.calls == [] and second.violations == result.violations
    assert [c.stage for c in store.model_calls("o/r", 1) if not c.cache_hit] == [
        "pr_plan",
        "pr_judge",
    ]


def test_judgment_cap_leaves_the_rule_for_fix(store) -> None:
    """PR 상한은 코드 위반 수(known)로만 는다. PR 단위 위반은 대상 위반 수에 들지 않는다."""
    calls = CallSettings(pr_base=0, per_violation=1, pr_max=5)
    model = Model({TESTING.id: [TESTING_PLAN]}, judge=[])
    result = pr_check.check([TESTING], _facts(), _tools(store, model, calls=calls), known=0)
    (r,) = result.results
    assert (r.status, r.kind) == ("left", "cap") and pr_check_pure.judge_left(result)
    assert model.stages() == ["plan"]
    resumed = pr_check.check([TESTING], _facts(), _tools(store, model, calls=calls), known=1)
    assert resumed.results[0].status == "checked" and model.stages() == ["plan", "judge"]


def test_judgment_failure_leaves_the_rule_and_is_not_stored(store) -> None:
    pr_check.check([TESTING], _facts(), _tools(store, Model({TESTING.id: [TESTING_PLAN]})))
    store_key_count = len(store.model_calls("o/r", 1))
    failing = Model({}, fail="judge")
    (r,) = pr_check.check([TESTING], _facts(body="새 본문"), _tools(store, failing)).results
    assert (r.status, r.kind) == ("left", "model_failed") and not pr_check_pure.judge_left(
        pr_check_pure.PrChecks((r,), ())
    )
    assert len(store.model_calls("o/r", 1)) > store_key_count


def test_unreadable_facts_leave_every_rule_unchecked_without_calls(store) -> None:
    model = Model({COMMITS.id: [COMMIT_PLAN]})
    result = pr_check.check([COMMITS, TESTING], "PR 사실을 읽지 못함: 404", _tools(store, model))
    assert [r.kind for r in result.results] == ["no_facts", "no_facts"]
    assert result.note and model.calls == []


def test_sandbox_failure_on_this_pr_is_unchecked_not_passed(store) -> None:
    """정규식이 시간 안에 끝나지 않으면 그 규칙은 "확인하지 못함"이다. 위반 없음으로 지나가지
    않는다."""
    pr_check.check([COMMITS], _facts(), _tools(store, Model({COMMITS.id: [COMMIT_PLAN]})))
    slow = ScriptSandbox(fail=Failed("정규식이 20초 안에 끝나지 않음"))
    (r,) = pr_check.check([COMMITS], _facts(), _tools(store, Model({}), slow)).results
    assert (r.status, r.kind) == ("unchecked", "run_failed") and "20초" in r.detail
    missing = ScriptSandbox(fail=ToolUnavailable("python"))
    (r,) = pr_check.check([COMMITS], _facts(), _tools(store, Model({}), missing)).results
    assert (r.status, r.kind) == ("unchecked", "tool_unavailable")


def test_cut_body_is_reported_as_partial(store) -> None:
    facts = PrFacts(title="t", body="설명", branch="b", commits=(), paths=("migrations/1.py",),
                    body_cut=True)  # fmt: skip
    model = Model({MIGRATION.id: [MIGRATION_PLAN]})
    result = pr_check.check([MIGRATION], facts, _tools(store, model))
    (r,) = result.results
    assert "앞부분만 봄" in r.partial
    assert result.violations == ()  # must_match가 잘린 본문에서 맞지 않은 것은 위반으로 내지 않는다


def test_document_examples_reach_the_plan_prompt(store) -> None:
    rule = _rule("R-readme-doc", "커밋 제목은 feat: 로 시작한다.",
                 [Example("feat: 로그인 추가", (), "document"),
                  Example("로그인 추가", (1,), "document")])  # fmt: skip
    plan = {**COMMIT_PLAN, "examples": [
        {"value": "feat: 로그인 추가", "changed_paths": [], "violates": False},
        {"value": "로그인 추가", "changed_paths": [], "violates": True},
        {"value": "fix: x", "changed_paths": [], "violates": False},
        {"value": "WIP", "changed_paths": [], "violates": True}]}  # fmt: skip
    model = Model({rule.id: [plan]})
    pr_check.check([rule], _facts(), _tools(store, model))
    assert "DOC_EXAMPLE n=1 지키는 예" in model.calls[0]["prompt"]
    assert "DOC_EXAMPLE n=2 어기는 예" in model.calls[0]["prompt"]


def test_pr_rules_come_only_from_unchecked_pr_outcomes() -> None:
    from app.worker.rules.doc_rules_pure import CallUsage, Found, RuleUnchecked

    usage = CallUsage(0, 0, 0, (), 0, 0)
    human = _rule("R-h", "리뷰어 승인을 받는다.")
    found = Found(
        (RuleUnchecked(COMMITS, "pr", ""), RuleUnchecked(human, "human", "")), (), 0, usage
    )
    assert pr_check.pr_rules(found) == (COMMITS,)
    assert pr_check.pr_rules(None) == ()
