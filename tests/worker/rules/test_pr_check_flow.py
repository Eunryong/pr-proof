"""PR 단위 확인의 사실 모으기와 상태 (docs/DESIGN.md 4.5 끝).

실제 git 저장소(로컬 원격, 커밋 여럿), 실제 SQLite. GitHub만 가짜다. 모델은 부르지 않는다.
"""

import subprocess
from pathlib import Path
from typing import Final

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.worker import graph, repo
from app.worker.rule_pure import CheckMethod, Rule, RuleSource
from app.worker.rules import pr_check, pr_check_pure
from app.worker.rules.pr_check_pure import PrChecks, PrPattern, PrRuleResult, PrUsage, PrViolation

from .test_doc_rules_flow import _git

COMMIT_RULE: Final = "커밋 메시지는 feat: 나 fix: 로 시작한다."
TESTING_RULE: Final = "PR 본문에 테스트 방법을 적는다."
README: Final = f"# demo\n\n## 기여\n\n- {COMMIT_RULE}\n- {TESTING_RULE}\n"


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


def test_gather_skips_pipeline_commits_by_recorded_merge(env, tmp_path: Path) -> None:
    """파이프라인 수정안 PR의 머지(기록한 머지 커밋)로 들어온 커밋은 보지 않는다. 메시지가 아니라
    기록한 해시로 가린다."""
    _, shas = _remote(
        tmp_path / "remote", ["feat: 하나", "[pipeline] model: #1 문서 규칙 위반 1건 수정"]
    )
    store = env["deps"].store
    store.record_proposal("o/r", 1, "model", shas[0], "pipeline/pr-1-fix-model", 900)
    (proposal,) = store.proposals("o/r", 1)
    store.set_proposal_state(proposal.id, "merged", merge_commit=shas[1])
    worktree = tmp_path / "wt-gather"
    _git(tmp_path, "clone", "-q", str(tmp_path / "remote"), str(worktree))
    repo.record_git_dir(worktree)
    facts = pr_check.gather(worktree, _git(worktree, "rev-parse", f"{shas[0]}^"), shas[1],
                            github=env["deps"].github, store=store, repo_name="o/r",
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
    assert isinstance(restored, PrChecks) and pr_check_pure.judge_left(restored)
    (result,) = restored.results
    assert isinstance(result.decl, PrPattern) and result.decl.fact == "pr_body"
    (violation,) = restored.violations
    assert isinstance(violation, PrViolation) and violation.sha == "a" * 40
    assert isinstance(restored.usage, PrUsage) and restored.usage.plan_calls == 1
