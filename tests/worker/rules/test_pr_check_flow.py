"""PR 단위 확인이 그래프에서 도는지 (docs/DESIGN.md 4.5 끝, 13절 4단계의 4-11–4-14).

실제 git 체크아웃(로컬 원격, 커밋 여럿), 실제 SQLite. 모델, 검사기 샌드박스(PR 패턴은 고정
스크립트를 이 프로세스에서 돌리는 대역), 테스트 실행, GitHub만 가짜다. Docker 없음.
"""

import subprocess
from pathlib import Path
from typing import Final

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from shared import state_keys_pure as state_keys
from shared.job_pure import PRJob
from worker import graph, main, repo, update
from worker.ai.claude_cli import StructuredResult
from worker.repo_config_pure import CallSettings, ModelSettings
from worker.rule_pure import CheckMethod, Rule, RuleSource
from worker.rules import pr_check
from worker.rules import pr_check_pure as pr_models
from worker.rules.pr_check_pure import PrChecks, PrPattern, PrRuleResult, PrUsage, PrViolation

from ..fix.test_model_fix_flow import AUTO, _command
from ..helpers import bodies
from .test_doc_rules_flow import (  # noqa: F401
    FakeModel,
    GrepSandbox,
    _git,
    _ok,
    doc_env,
    rule_answer,
    use_config,
    use_model,
)
from .test_pr_check import COMMIT_PLAN, TESTING_PLAN, ScriptSandbox

COMMIT_RULE: Final = "커밋 메시지는 feat: 나 fix: 로 시작한다."
TESTING_RULE: Final = "PR 본문에 테스트 방법을 적는다."
README: Final = f"# demo\n\n## 기여\n\n- {COMMIT_RULE}\n- {TESTING_RULE}\n"


def _pr_rule(quote: str) -> dict:
    return {"file": "README.md", "section": "기여", "quote": quote, "target": "pr", "scope": [],
            "scope_exclude": [], "scope_reason": "", "examples": [], "check": "none",
            "reason": "PR 규칙", "fix": "none"}  # fmt: skip


class PrModel(FakeModel):
    """FakeModel에 PR 패턴 작성과 PR 단위 판단의 답을 더한다."""

    def __init__(self, judge: list[dict] | None = None, **kwargs) -> None:
        super().__init__(rule_answer(_pr_rule(COMMIT_RULE), _pr_rule(TESTING_RULE)), **kwargs)
        self.judge = judge or []

    def __call__(self, prompt: str, *, schema, **kw) -> StructuredResult:
        if schema is pr_models.PLAN_SCHEMA:
            self.prompts.append(("pr_plan", prompt))
            return _ok(COMMIT_PLAN if COMMIT_RULE in prompt else TESTING_PLAN)
        if schema is pr_models.JUDGE_SCHEMA:
            self.prompts.append(("pr_judge", prompt))
            return _ok({"violations": self.judge})
        return super().__call__(prompt, schema=schema, **kw)


class PrSandbox(GrepSandbox):
    def __init__(self) -> None:
        super().__init__()
        self.script = ScriptSandbox()

    def pr_pattern(self, decl, cases):
        return self.script.pr_pattern(decl, cases)


def _remote(path: Path, messages: list[str]) -> tuple[str, list[str]]:
    """base 커밋과 PR 커밋들. (base, 커밋들 오래된 것부터)."""
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "uploadpack.allowAnySHA1InWant", "true")
    (path / "README.md").write_text(README)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "base")
    base = _git(path, "rev-parse", "HEAD")
    shas = []
    for n, message in enumerate(messages):
        (path / f"f{n}.txt").write_text(f"{n}\n")
        _git(path, "add", "-A")
        command = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q",
                   "-m", message]  # fmt: skip
        subprocess.run(command, cwd=path, check=True, capture_output=True)
        shas.append(_git(path, "rev-parse", "HEAD"))
    _git(path, "update-ref", "refs/pull/1/head", shas[-1])
    return base, shas


def _run(env, tmp_path: Path, messages: list[str], body: str = "고쳤습니다"):
    base, shas = _remote(tmp_path / "remote", messages)
    env["gh"].head, env["gh"].base, env["gh"].body = shas[-1], base, body
    env["redis"].set(state_keys.latest_head("o/r", 1), shas[-1])
    sandbox = PrSandbox()
    env["monkeypatch"].setattr(graph.doc_rules, "sandbox_for", lambda *a: sandbox)
    job = PRJob(repo="o/r", pr_number=1, kind="update", head_sha=shas[-1], base_sha=base,
                source="webhook")  # fmt: skip
    deps = env["deps"]
    return graph.run_flow(job, update._context(deps, deps.store.start_run(job), None)), shas


def _kinds(model: FakeModel) -> list[str]:
    return [kind for kind, _ in model.prompts]


def test_pr_rules_are_checked_after_admission_and_shown_apart(doc_env, tmp_path: Path) -> None:  # noqa: F811
    """4-11: 형식을 어긴 커밋만 잡힌다. 4-13: PR 단위 위반은 `doc_rules`(수정 경로가 읽는 값)에
    들어가지 않고 수정 판단도 돌지 않는다. 결과 코멘트의 따로 된 부분에 나온다."""
    judge = [{"where": "pr_body", "sha": "", "quote": "", "why": "테스트 방법이 없음"}]
    model = use_model(doc_env, PrModel(judge))
    state, shas = _run(doc_env, tmp_path, ["feat: 하나", "update", "fix: 셋"])
    checks = state["pr_checks"]
    assert [(v.where, v.sha) for v in checks.violations] == [("commit", shas[1]), ("pr_body", "")]
    assert state["doc_rules"].violations == () and "fix_decisions" not in state
    assert _kinds(model) == ["rules", "pr_plan", "pr_plan", "pr_judge"]
    assert doc_env["gh"].text_lookups == 1
    comment = bodies(doc_env["gh"])[-1]
    assert "⚠️ PR 단위 위반 2건 (규칙 2개)" in comment
    assert f"  - 커밋 `{shas[1][:7]}` `update`" in comment
    assert "  - PR 본문 (빠진 내용) · `테스트 방법이 없음`" in comment
    assert "아직 확인하지 않음" not in comment


def test_no_pr_rules_means_no_pr_text_is_read(doc_env, tmp_path: Path) -> None:  # noqa: F811
    model = use_model(doc_env, FakeModel(rule_answer()))
    state, _ = _run(doc_env, tmp_path, ["update"])
    assert "pr_checks" not in state and doc_env["gh"].text_lookups == 0
    assert "pr_plan" not in _kinds(model)


def test_pr_judgment_cap_is_shown_and_fix_resumes_it(doc_env, tmp_path: Path) -> None:  # noqa: F811
    """PR 상한에 걸린 PR 단위 판단은 "판단하지 못함"으로 남고, `/fix`가 PR 상한을 한 번 더 받아 PR
    단위 확인부터 잇는다. 이미 정한 계획은 다시 묻지 않는다."""
    use_config(doc_env, AUTO.model_copy(update={"model": ModelSettings(
        calls=CallSettings(pr_base=0))}))  # fmt: skip
    model = use_model(doc_env, PrModel())
    state, _ = _run(doc_env, tmp_path, ["feat: 하나"])
    assert pr_models.judge_left(state["pr_checks"]) and "pr_judge" not in _kinds(model)
    comment = bodies(doc_env["gh"])[-1]
    assert "모델 호출 상한에 걸려 처리하지 못함: PR 단위 판단 1개 규칙" in comment

    deps = doc_env["deps"]
    assert main.handle(_command("/fix", 80), deps) == "fix:no_targets"
    assert _kinds(model).count("pr_judge") == 1 and _kinds(model).count("pr_plan") == 2
    comment = bodies(doc_env["gh"])[-1]
    assert "PR 단위 판단" not in comment and "PR 단위 규칙 2개 위반 없음" in comment
    assert main.handle(_command("/fix", 81), deps) == "no_pending_fix"


def test_gather_skips_pipeline_commits_by_recorded_merge(doc_env, tmp_path: Path) -> None:  # noqa: F811
    """파이프라인 수정안 PR의 머지(기록한 머지 커밋)로 들어온 커밋은 보지 않는다. 메시지가 아니라
    기록한 해시로 가린다."""
    use_model(doc_env, PrModel())
    _, shas = _run(
        doc_env, tmp_path, ["feat: 하나", "[pipeline] model: #1 문서 규칙 위반 1건 수정"]
    )
    store = doc_env["deps"].store
    store.record_proposal("o/r", 1, "model", shas[0], "pipeline/pr-1-fix-model", 900)
    (proposal,) = store.proposals("o/r", 1)
    store.set_proposal_state(proposal.id, "merged", merge_commit=shas[1])
    worktree = tmp_path / "wt-gather"
    _git(tmp_path, "clone", "-q", str(tmp_path / "remote"), str(worktree))
    repo.record_git_dir(worktree)
    facts = pr_check.gather(worktree, _git(worktree, "rev-parse", f"{shas[0]}^"), shas[1],
                            github=doc_env["deps"].github, store=store, repo_name="o/r",
                            pr_number=1)  # fmt: skip
    assert [c.sha for c in facts.checked_commits] == [shas[0]]
    assert facts.commits[0].pipeline and facts.title == "PR 제목"


def test_pr_checks_survive_checkpoint() -> None:
    rule = Rule(id="R-c", text=COMMIT_RULE, source=RuleSource("README.md", "기여", "b"),
                origin="document", scope=(), check=CheckMethod("none"), fix="none",
                target="pr")  # fmt: skip
    checks = PrChecks(
        (PrRuleResult(rule, "left", "model", PrPattern("pr_body"), "model_said", kind="cap",
                      detail="PR 상한"),),
        (PrViolation("R-c", "commit", "pattern", "a" * 40, "update", "패턴"),),
        commits=2, usage=PrUsage(plan_calls=1, models=("m",)),
    )  # fmt: skip
    serde = JsonPlusSerializer(allowed_msgpack_modules=list(graph._STATE_TYPES))
    restored = serde.loads_typed(serde.dumps_typed({"pr_checks": checks}))["pr_checks"]
    assert isinstance(restored, PrChecks) and pr_models.judge_left(restored)
    (result,) = restored.results
    assert isinstance(result.decl, PrPattern) and result.decl.fact == "pr_body"
    (violation,) = restored.violations
    assert isinstance(violation, PrViolation) and violation.sha == "a" * 40
    assert isinstance(restored.usage, PrUsage) and restored.usage.plan_calls == 1
