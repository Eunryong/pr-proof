"""/fix static 흐름 (3단계): static 검출 → 커버리지 → 제안 → 승인 → AI 수정 → 비밀값 검사 → 제안 PR.

AI 루프 자체(ai_fix.code_fix)는 가짜로 바꾼다. 루프의 범위 검사·재시도·라우팅은 test_ai_fix.py가
실제 git으로 본다. 여기서는 그래프가 스킬별로 제안·대기·재개·코멘트를 맞게 하는지 본다.
"""

import dataclasses
from dataclasses import replace
from typing import Final

import pytest

from app.models.finding import Finding, SkillResult
from app.models.fix import FixOutcome
from app.models.job import PRJob
from app.models.repo_config import FixSettings, RepoConfig, SkillsSettings
from app.models.test_run import PytestRun, ReportSummary
from app.worker import ai_fix, base_config, coverage, graph, main, repo, update
from app.worker.ai.claude_cli import ClaudeResult
from app.worker.ai_fix import AIEnvironmentError
from app.worker.skills import static

from .helpers import BASE, HEAD, bodies, use_runner
from .test_fix_flow import _fix_command, _replies, _update, fix_env  # noqa: F401

MAGIC: Final = Finding(
    "src/app.py", 3, "warning", "Magic value `3`", "static", "기본 규칙", "PLR2004"
)
REPEAT: Final = Finding(
    "src/app.py", 7, "warning", "repeated `'x'`", "static", "기본 규칙", "STATIC-REPEAT"
)


@pytest.fixture
def static_env(fix_env):  # noqa: F811
    """lint 위반 2건(fix_env)에 static 위반 2건. MAGIC은 테스트가 지나가고 REPEAT은 아니다."""
    mp = fix_env["monkeypatch"]
    mp.setattr(
        static, "run_static", lambda *a, **k: SkillResult("static", (MAGIC, REPEAT), "기본 규칙")
    )

    def annotate(findings, coverage_xml, worktree):
        return tuple(replace(f, covered=f.line == MAGIC.line) for f in findings)

    mp.setattr(coverage, "annotate_coverage", annotate)
    calls: list[dict] = []
    state = {"calls": calls, "explanation": "3을 THRESHOLD 상수로 뺐습니다", "raise": None}

    def fake_code_fix(worktree, merge_base, skill, before, targets, **kw):
        calls.append({"skill": skill, "targets": list(targets), "spent": kw["spent_usd"]})
        if state["raise"] is not None:
            error, state["raise"] = state["raise"], None
            raise error
        result = ClaudeResult(ok=True, error=None, cost_usd=0.01, num_turns=2,
                              permission_denials=(), text="ok")  # fmt: skip
        kw["tools"].record_call(1, "haiku", result, 0.01)
        return FixOutcome(
            skill=skill, status="ready", fixed=len(targets), remaining=(REPEAT,),
            test_run=kw["test_before"], changed_files=("src/app.py",),
            explanation=state["explanation"], ai_calls=1, ai_cost_usd=0.01, models=("haiku",),
        )  # fmt: skip

    mp.setattr(ai_fix, "code_fix", fake_code_fix)
    mp.setattr(repo, "head_diff", lambda wt: "+THRESHOLD = 3\n")
    fix_env["static"] = state
    return fix_env


def _target() -> PRJob:
    return PRJob.model_validate_json(_update())


def test_static_violations_are_shown_and_offered(static_env) -> None:
    assert main.handle(_update(), static_env["deps"]) == "passed"
    body = bodies(static_env["gh"])[-1]
    assert (
        "**규칙:** ⚠️ 위반 4건 (lint 2, static 2) · 자동 수정 가능: `/fix lint`, `/fix static`"
        in body
    )
    assert "- `src/app.py:3` `PLR2004` Magic value `3`" in body
    assert (
        "자동 수정 대상이 아닌 위반:\n- `src/app.py:7` `STATIC-REPEAT` 테스트가 지나가지 않아"
        in body
    )
    assert "AI가 테스트가 지나가는 static 위반 1건을 고치고" in body
    assert "정적변수 (기본 규칙)" in body
    assert update.has_pending_fix(_target(), static_env["deps"], "static")
    assert update.has_pending_fix(_target(), static_env["deps"], "lint")
    assert static_env["static"]["calls"] == []  # 승인 전에는 AI를 부르지 않는다


def test_fix_static_opens_ai_proposal_then_lint_still_waits(static_env) -> None:
    deps, gh = static_env["deps"], static_env["gh"]
    main.handle(_update(), deps)
    assert main.handle(_fix_command(77, "/fix static"), deps) == "fix:ready"
    (call,) = static_env["static"]["calls"]
    assert call["targets"] == [replace(MAGIC, covered=True)]  # 테스트가 지나가는 위반만
    ((number, pull),) = gh.pulls.items()
    assert pull["head"] == "pipeline/pr-1-fix-static"
    assert "AI가 고친 수정안" in pull["body"] and "3을 THRESHOLD 상수로 뺐습니다" in pull["body"]
    body = bodies(gh)[-1]
    assert (
        f"**static 수정안:** 위반 1건을 고쳤습니다. 고친 뒤 테스트: 통과 (9개). 제안 PR #{number}"
        in body
    )
    assert "AI 호출 1회 (haiku) · 추정 비용 $0.0100" in body
    assert "AI의 변경 설명" in body
    assert "자동 수정 가능: `/fix lint`" in body  # lint는 아직 기다린다
    assert deps.store.ai_spent("o/r", 1) == pytest.approx(0.01)
    # 이어서 lint도 고칠 수 있다
    assert main.handle(_fix_command(78, "/fix lint"), deps) == "fix:ready"
    body = bodies(gh)[-1]
    assert "**static 수정안:**" in body and "**lint 수정안:**" in body
    assert "자동 수정 가능" not in body
    assert main.handle(_fix_command(79, "/fix static"), deps) == "no_pending_fix"
    assert "static 제안 PR" in _replies(gh)[-1]


def test_uncovered_only_is_not_offered(static_env) -> None:
    static_env["monkeypatch"].setattr(
        static, "run_static", lambda *a, **k: SkillResult("static", (REPEAT,), "기본 규칙")
    )
    main.handle(_update(), static_env["deps"])
    body = bodies(static_env["gh"])[-1]
    assert "`/fix static`" not in body and "테스트가 지나가지 않아 자동 수정 대상 아님" in body
    assert not update.has_pending_fix(_target(), static_env["deps"], "static")
    assert main.handle(_fix_command(77, "/fix static"), static_env["deps"]) == "no_pending_fix"
    assert "승인을 기다리는 static 수정이 없습니다" in _replies(static_env["gh"])[-1]


def test_flaky_tests_block_static_but_not_lint(static_env) -> None:
    flaky = PytestRun(
        exit_code=0, summary=ReportSummary(passed=8, failed=1, total=9, failed_tests=("t::a",)),
        flaky=("t::a",),
    )  # fmt: skip
    use_runner(static_env, lambda *a, **k: flaky)
    main.handle(_update(), static_env["deps"])
    body = bodies(static_env["gh"])[-1]
    assert "static 자동 수정을 제안하지 않습니다 (" in body
    assert "자동 수정 가능: `/fix lint`" in body and "`/fix static`" not in body


def test_secret_in_ai_explanation_drops_the_whole_proposal(static_env) -> None:
    static_env["static"]["explanation"] = "읽은 키: ghp_" + "a" * 36
    main.handle(_update(), static_env["deps"])
    assert main.handle(_fix_command(77, "/fix static"), static_env["deps"]) == "fix:secret"
    assert static_env["gh"].pulls == {} and static_env["state"]["pushes"] == []
    body = bodies(static_env["gh"])[-1]
    assert "**static 수정안:** 수정안을 버림: 비밀값으로 보이는 내용이 있음 (토큰·키 형식)" in body
    assert "ghp_" not in body and "+THRESHOLD" not in body  # 설명도 diff도 싣지 않는다
    patches = list((static_env["deps"].settings.data_dir / "fixes").rglob("*-static.patch"))
    assert patches == []


def test_secret_in_ai_diff_is_dropped_even_for_fork(static_env) -> None:
    static_env["gh"].head_repo = "someone/r"  # 포크: 제안 PR 없이 코멘트에만 보이는 경우
    static_env["monkeypatch"].setattr(
        repo, "head_diff", lambda wt: "+K = 'sk-ant-" + "a" * 30 + "'\n"
    )
    main.handle(_update(), static_env["deps"])
    assert main.handle(_fix_command(77, "/fix static"), static_env["deps"]) == "fix:secret"
    assert "sk-ant-" not in bodies(static_env["gh"])[-1]


def test_ai_environment_error_is_retried_from_the_fix(static_env) -> None:
    from app.worker import retry

    main.handle(_update(), static_env["deps"])
    static_env["static"]["raise"] = AIEnvironmentError("claude 로그인이 풀림")
    static_env["monkeypatch"].setattr(retry, "attempt", lambda: (1, False))
    with pytest.raises(AIEnvironmentError):
        main.handle(_fix_command(77, "/fix static"), static_env["deps"])
    assert "환경 문제로 대기 중" in bodies(static_env["gh"])[-1]
    assert not static_env["deps"].store.command_handled(77)
    assert main.handle(_fix_command(77, "/fix static"), static_env["deps"]) == "fix:ready"


def test_auto_mode_fixes_both_skills_in_fresh_worktrees(static_env) -> None:
    repo_config = RepoConfig(skills=SkillsSettings(fix=FixSettings(require_approval=False)))
    base = dataclasses.replace(BASE, repo=repo_config)
    mp = static_env["monkeypatch"]
    mp.setattr(base_config, "load", lambda *a, **k: base)
    checkouts: list[str] = []
    real = repo.checkout

    def counting(*a, **k):
        checkouts.append(a[3])
        return real(*a, **k)

    mp.setattr(repo, "checkout", counting)
    assert main.handle(_update(), static_env["deps"]) == "passed"
    body = bodies(static_env["gh"])[-1]
    assert "**lint 수정안:**" in body and "**static 수정안:**" in body
    # 첫 수정은 검사한 worktree를 그대로 쓰고, 두 번째 수정은 새 worktree에서 한다
    assert len(checkouts) == 2
    assert len(static_env["gh"].pulls) == 2


def test_static_disabled_skips_detection(static_env) -> None:
    repo_config = RepoConfig(skills=SkillsSettings(enabled=["lint"]))
    base = dataclasses.replace(BASE, repo=repo_config)
    mp = static_env["monkeypatch"]
    mp.setattr(base_config, "load", lambda *a, **k: base)
    mp.setattr(static, "run_static", lambda *a, **k: pytest.fail("static이 돌면 안 됨"))
    main.handle(_update(), static_env["deps"])
    assert "이 리포에서 꺼진 규칙 스킬: static" in bodies(static_env["gh"])[-1]


def test_min_repeats_from_pipeline_yaml_is_passed(static_env) -> None:
    from app.models.repo_config import StaticSettings

    repo_config = RepoConfig(skills=SkillsSettings(static=StaticSettings(min_repeats=5)))
    base = dataclasses.replace(BASE, repo=repo_config)
    mp = static_env["monkeypatch"]
    mp.setattr(base_config, "load", lambda *a, **k: base)
    seen: dict = {}

    def run_static(worktree, base, **kw):
        seen.update(kw)
        return SkillResult("static", (), kw["source"])

    mp.setattr(static, "run_static", run_static)
    main.handle(_update(), static_env["deps"])
    assert (seen["min_repeats"], seen["source"]) == (5, ".pipeline.yaml")


def test_checkpoint_from_before_stage3_still_resumes_lint() -> None:
    assert graph._awaiting_skills({"skill": "lint", "head_sha": HEAD}) == ["lint"]
    assert graph._awaiting_skills({"skills": ["lint", "static"]}) == ["lint", "static"]
    assert graph._awaiting_skills(None) == []
