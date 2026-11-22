"""모델의 직접 판단이 그래프에서 도는지 (docs/DESIGN.md 4.5, 13절 4단계의 4-2, 4-4, 4-6).

실제 git 체크아웃(로컬 원격), 실제 모델 전용 폴더와 git apply, 실제 SQLite. 모델(claude)과 검사기
샌드박스, 테스트 실행, GitHub, 푸시만 가짜다. Docker 없음.
"""

import json
import re
from pathlib import Path
from typing import Final

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.shared import state_keys_pure
from app.shared.job_pure import PRJob
from app.worker import graph, main, update
from app.worker.ai.claude_cli import StructuredResult
from app.worker.checker_authoring_pure import ToJudge, Unchecked
from app.worker.doc_rules_pure import CallUsage, Found, RuleJudged, RuleUnchecked
from app.worker.repo_config_pure import CallSettings, ModelSettings
from app.worker.rule_pure import CheckMethod, Rule, RuleSource, rule_id
from app.worker.rules import doc_rules, judge_pure
from app.worker.rules.judge_pure import JudgeUsage

from ..fix.test_model_fix_flow import (  # noqa: F401
    AUTO,
    BRANCH,
    FakeFixer,
    FakeModel,
    _command,
    fix_env,
    use_fixer,
)
from ..helpers import bodies
from .test_doc_rules_flow import (  # noqa: F401
    _covered_tests,
    _git,
    _ok,
    checker_answer,
    doc_env,
    make_remote,
    print_rule,
    rule_answer,
    use_config,
    use_model,
)

NAME_RULE: Final = "함수 이름은 하는 일을 동사로 시작한다."
NAME_ID: Final = rule_id("README.md", NAME_RULE)
README: Final = f"# demo\n\n## 코드 규칙\n\n### 이름\n\n- {NAME_RULE}\n"
BASE: Final = {"README.md": README, "orders/a.py": "x = 1\n"}
PR: Final = {"orders/a.py": "x = 1\ndef total(order):\n    return order\n"}
FLAG: Final = {"rule_id": NAME_ID, "file": "orders/a.py", "line": 2, "quote": "def total(order):",
               "why": "명사로 시작함"}  # fmt: skip
RENAME: Final = {"orders/a.py": [("def total(", "def compute_total(")]}
EXPLANATION: Final = json.dumps({
    "summary": "함수 이름을 동사로 시작하게 바꿈",
    "changes": [{"file": "orders/a.py", "rule": NAME_ID, "why": "동사로 시작한다"}],
}, ensure_ascii=False)  # fmt: skip


def name_rule() -> dict:
    return print_rule(section="코드 규칙 > 이름", quote=NAME_RULE, scope=[])


class JudgeModel(FakeModel):
    """FakeModel에 모델 판단의 답을 더한다: 프롬프트에 든 파일마다 flags의 답."""

    def __init__(self, *args, flags: list[dict] | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.flags = [FLAG] if flags is None else flags

    def __call__(self, prompt: str, *, schema, **kw) -> StructuredResult:
        if schema is judge_pure.ANSWER_SCHEMA:
            self.prompts.append(("judge", prompt))
            files = set(re.findall(r"^파일: (.+)$", prompt, re.M))
            return _ok({"violations": [f for f in self.flags if f["file"] in files]})
        return super().__call__(prompt, schema=schema, **kw)


def _model(**kwargs) -> JudgeModel:
    """검사기 작성은 두 번 모두 예시를 통과하지 못한다 (grep이 아무 줄도 잡지 않는다)."""
    return JudgeModel(rule_answer(name_rule()), checker=checker_answer("grep:NOPE"), **kwargs)


def _run(env, tmp_path: Path, pr: dict[str, str] = PR, pr_number: int = 1):
    """PR의 커밋 하나를 그래프로 돌린다. 두 번째 부르면 같은 PR 브랜치에 새 커밋을 올린다."""
    remote = tmp_path / "remote"
    if remote.exists():
        for name, text in pr.items():
            (remote / name).write_text(text)
        _git(remote, "add", "-A")
        _git(remote, "commit", "-qm", "next")
        head = _git(remote, "rev-parse", "HEAD")
        _git(remote, "update-ref", f"refs/pull/{pr_number}/head", head)
        base_sha = env["gh"].base
    else:
        base_sha, head = make_remote(remote, BASE, pr)
    env["gh"].head, env["gh"].base = head, base_sha
    env["redis"].set(state_keys_pure.latest_head("o/r", pr_number), head)
    job = PRJob(repo="o/r", pr_number=pr_number, kind="update", head_sha=head, base_sha=base_sha,
                source="webhook")  # fmt: skip
    deps = env["deps"]
    return graph.run_flow(job, update.flow_context(deps, deps.store.start_run(job), None))


def _kinds(model: FakeModel) -> list[str]:
    return [kind for kind, _ in model.prompts]


def test_rule_without_checker_is_judged_and_fixed(fix_env, tmp_path: Path) -> None:  # noqa: F811
    """4-4: 검사기가 예시 확인을 두 번 통과하지 못한 규칙이 모델 판단으로 간다. 4-2: 그 줄이
    `found_by: model`로 나오고 수정 판단과 모델 수정을 거쳐 수정안 PR까지 가며, 본문에 "도구로
    재검사하지 못함"이 적힌다."""
    model = use_model(fix_env, _model())
    use_fixer(fix_env, FakeFixer([RENAME], [EXPLANATION]))
    state = _run(fix_env, tmp_path)

    assert _kinds(model) == ["rules", "tool", "checker", "checker", "judge", "decide"]
    found = state["doc_rules"]
    (outcome,) = found.outcomes
    assert isinstance(outcome, RuleJudged) and outcome.left == 0 and outcome.route == "examples"
    assert outcome.reason.startswith("예시 확인을 2번 통과하지 못함")
    (v,) = found.violations
    assert (v.rule_id, v.file, v.line, v.found_by, v.covered) == (
        NAME_ID, "orders/a.py", 2, "model", True
    )  # fmt: skip
    judge_prompt = dict(model.prompts)["judge"]
    assert NAME_RULE in judge_prompt and "     2 + def total(order):" in judge_prompt
    assert found.judge == JudgeUsage(rules=1, hunks=1, calls=1, cached=0, dropped=0,
                                     input_tokens=1000, output_tokens=100,
                                     models=("claude-haiku-5-5",))  # fmt: skip

    fix = state["model_fix"]
    assert fix.ready and fix.verification.unverified == 1
    ((_, pull),) = fix_env["gh"].pulls.items()
    assert pull["head"] == BRANCH
    assert "이 규칙은 도구로 재검사하지 못함" in pull["body"]
    comment = bodies(fix_env["gh"])[-1]
    assert "모델 판단 (검사기가 예시 확인을 두 번 통과하지 못함)" in comment
    assert "모델 판단: 규칙 1개, hunk 1개 · 모델 호출 1회 · 저장된 판단 0건 사용" in comment
    assert "읽었지만 아직 검사하지 못하는" not in comment


def test_fix_that_leaves_the_judged_line_unchanged_is_retried(fix_env, tmp_path: Path) -> None:  # noqa: F811
    """4.8의 5번: 모델이 찾은 위반은 그 줄이 바뀌었는지만 본다. 바뀌지 않으면 남은 위반이다."""
    use_model(fix_env, _model())
    other = {"orders/a.py": [("x = 1\n", "x = 2\n")]}
    fixer = use_fixer(fix_env, FakeFixer([other], [EXPLANATION]))
    state = _run(fix_env, tmp_path)
    assert state["model_fix"].status == "failed" and len(fixer.calls) == 3
    assert f"아직 남은 위반: orders/a.py:2 {NAME_ID}" in fixer.calls[1]["prompt"]


def test_same_hunk_on_another_commit_is_judged_from_the_stored_answer(doc_env, tmp_path) -> None:  # noqa: F811
    """4-2: 같은 hunk를 가진 다음 커밋은 모델 판단을 다시 부르지 않고 같은 답을 쓴다."""
    model = use_model(doc_env, _model(decide=lambda ids: [
        {"id": i, "action": "skip", "why": "케이스"} for i in ids]))  # fmt: skip
    first = _run(doc_env, tmp_path)["doc_rules"]
    asked = _kinds(model).count("judge")
    second = _run(doc_env, tmp_path, {**PR, "notes.txt": "다음 커밋\n"})["doc_rules"]
    assert _kinds(model).count("judge") == asked == 1
    assert [(v.id, v.line, v.found_by) for v in second.violations] == [
        (v.id, v.line, v.found_by) for v in first.violations
    ]
    assert second.judge.calls == 0 and second.judge.cached == 1


def test_judge_cap_is_shown_and_fix_continues(fix_env, tmp_path: Path) -> None:  # noqa: F811
    """4-6: PR 상한에 걸려 모델 판단을 하지 못한 규칙 수가 코멘트에 나오고, `/fix`가 PR 상한을 한
    번 더 받아 모델 판단부터 이어 한다(테스트도 다시 돌려 새 위반의 커버 여부를 본다)."""
    model = use_model(fix_env, _model())
    use_config(fix_env, AUTO.model_copy(update={"model": ModelSettings(
        calls=CallSettings(pr_base=0))}))  # fmt: skip
    fixer = use_fixer(fix_env, FakeFixer([RENAME], [EXPLANATION]))
    state = _run(fix_env, tmp_path)
    (outcome,) = state["doc_rules"].outcomes
    assert isinstance(outcome, RuleJudged) and (outcome.left, outcome.left_kind) == (1, "cap")
    assert state["doc_rules"].violations == () and "judge" not in _kinds(model)
    comment = bodies(fix_env["gh"])[-1]
    assert "모델 호출 상한에 걸려 처리하지 못함: 위반 판단 1개 규칙 (hunk 1개" in comment
    assert "`/fix`로 다시 돌릴 수 있습니다." in comment
    assert "**규칙:** 확인한 규칙 없음 (읽은 규칙 1개)" in comment

    deps = fix_env["deps"]
    assert main.handle(_command("/fix", 80), deps) == "fix:ready"
    assert _kinds(model).count("judge") == 1 and len(fixer.calls) == 1
    assert deps.store.call_grants("o/r", 1) == 1
    comment = bodies(fix_env["gh"])[-1]
    assert "위반 판단" not in comment and "수정안 PR #900을 열었습니다." in comment
    assert main.handle(_command("/fix", 81), deps) == "no_pending_fix"


def test_judge_model_failure_is_listed_and_not_stored(doc_env, tmp_path: Path) -> None:  # noqa: F811
    failed = StructuredResult(ok=False, error=None, data=None, model_id="", input_tokens=None,
                              output_tokens=None, cost_usd=None, permission_denials=0,
                              detail="시간 초과")  # fmt: skip

    class Failing(JudgeModel):
        def __call__(self, prompt: str, *, schema, **kw) -> StructuredResult:
            if schema is judge_pure.ANSWER_SCHEMA:
                self.prompts.append(("judge", prompt))
                return failed
            return super().__call__(prompt, schema=schema, **kw)

    failing = Failing(rule_answer(name_rule()), checker=checker_answer("grep:NOPE"))
    model = use_model(doc_env, failing)
    state = _run(doc_env, tmp_path)
    (outcome,) = state["doc_rules"].outcomes
    assert outcome.left_kind == "model_failed"
    assert "모델 판단을 하지 못한 규칙 1개" in bodies(doc_env["gh"])[-1]
    use_model(doc_env, model)
    assert _kinds(model).count("judge") == 1


def test_path_rule_without_checker_is_not_judged(doc_env, tmp_path: Path) -> None:  # noqa: F811
    path_rule = print_rule(section="코드 규칙 > 이름", quote=NAME_RULE, scope=[], check="path")
    model = use_model(doc_env, JudgeModel(rule_answer(path_rule), checker=checker_answer("x")))
    found = _run(doc_env, tmp_path)["doc_rules"]
    assert not any(isinstance(o, RuleJudged) for o in found.outcomes)
    assert "judge" not in _kinds(model)


def test_judged_rule_state_survives_checkpoint() -> None:
    rule = Rule(id=NAME_ID, text=NAME_RULE, source=RuleSource("README.md", "코드 규칙 > 이름", "b"),
                origin="document", scope=(), check=CheckMethod("checker"),
                fix="model")  # fmt: skip
    usage = JudgeUsage(rules=1, hunks=2, calls=1, cached=1, dropped=0, models=("m",))
    judged = RuleJudged(rule, "주석의 뜻", "none", 1, "cap", "PR 상한")
    found = Found((judged,), (), 0, CallUsage(0, 10, 0, (), 0, 0), judge=usage)
    serde = JsonPlusSerializer(allowed_msgpack_modules=list(graph._STATE_TYPES))
    restored = serde.loads_typed(serde.dumps_typed({"doc_rules": found}))["doc_rules"]
    (outcome,) = restored.outcomes
    assert isinstance(outcome, RuleJudged) and outcome.left_kind == "cap"
    assert outcome.route == "none"
    assert isinstance(restored.judge, JudgeUsage) and restored.judge.cached == 1


def test_routed_rule_becomes_judged_with_its_route_and_path_rules_stay_unchecked() -> None:
    rule = Rule(id=NAME_ID, text=NAME_RULE, source=RuleSource("README.md", "코드 규칙 > 이름", "b"),
                origin="document", scope=(), check=CheckMethod("checker"),
                fix="model")  # fmt: skip
    models = ModelSettings()
    judged = doc_rules._outcome(ToJudge(rule, "part", "뜻을 읽어야 한다"), models, None)
    assert judged == RuleJudged(rule, "뜻을 읽어야 한다", "part")
    path_rule = Rule(id="R-p", text="테스트 파일 이름은 test_로 시작한다.",
                     source=RuleSource("README.md", "테스트", "b"), origin="document",
                     scope=(), check=CheckMethod("path"), fix="none")  # fmt: skip
    left = doc_rules._outcome(Unchecked(path_rule, "no_checker", "3회 실패"), models, None)
    assert left == RuleUnchecked(path_rule, "no_checker", "3회 실패")
    # 수정 뒤 재검사(4.8의 5번)는 모델 판단으로 가는 규칙을 도구·검사기로 다시 돌리지 않는다
    found = Found((judged,), (), 0, CallUsage(0, 10, 0, (), 0, 0))
    assert doc_rules.saved_checks(found, store=None) == [ToJudge(rule, "part", "뜻을 읽어야 한다")]
