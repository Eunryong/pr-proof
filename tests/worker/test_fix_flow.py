"""/fix lint 흐름: 제안 → 승인 대기(체크포인트) → /fix lint로 재개 → 수정 → 검증 → 코멘트.

Docker·GitHub 없이 가짜 lint·테스트·git으로 확인한다. 실제 ruff 수정은 skills/test_lint.py의
docker 테스트가 본다.
"""

import dataclasses
import json
from typing import Final

import pytest

from app.models.finding import Finding, SkillResult
from app.models.job import PRJob
from app.models.repo_config import FixSettings, RepoConfig, SkillsSettings
from app.models.test_run import PytestRun, ReportSummary
from app.worker import base_config, main, repo
from app.worker.skills import lint
from app.worker.skills.lint import LintFix

from .helpers import BASE, HEAD, PASSED, bodies, use_runner
from .test_github import is_reply

FIXABLE: Final = Finding("a.py", 2, "error", "`sys` imported but unused", "lint", "x", "F401", True)
UNFIXABLE: Final = Finding("a.py", 5, "error", "ambiguous name", "lint", "x", "E741", False)


def _update() -> str:
    return PRJob(
        repo="o/r", pr_number=1, kind="update", head_sha=HEAD, base_sha="b" * 40, source="webhook"
    ).model_dump_json()


def _fix_command(comment_id: int = 77, text: str = "/fix lint") -> str:
    return PRJob(
        repo="o/r", pr_number=1, kind="command", command=text, comment_id=comment_id,
        source="webhook",
    ).model_dump_json()  # fmt: skip


@pytest.fixture
def fix_env(env):
    mp = env["monkeypatch"]
    mp.setattr(lint, "run_lint", lambda *a, **k: SkillResult("lint", (FIXABLE, UNFIXABLE), "x"))
    state = {"fix_calls": 0, "status": " M a.py\0", "after": (UNFIXABLE,)}

    def fake_fix(worktree, base, merge_base, **kwargs):
        state["fix_calls"] += 1
        after = SkillResult("lint", state["after"], "x")
        return LintFix(after=after, applied=1, touched_files=("a.py",))

    mp.setattr(lint, "fix_lint", fake_fix)
    mp.setattr(repo, "status", lambda wt: state["status"])
    mp.setattr(repo, "head_diff", lambda wt: "-import sys\n")
    use_runner(env, lambda *a, **k: PASSED)
    env["state"] = state
    return env


def _with_approval(env, required: bool) -> None:
    repo_config = RepoConfig(skills=SkillsSettings(fix=FixSettings(require_approval=required)))
    base = dataclasses.replace(BASE, repo=repo_config)
    env["monkeypatch"].setattr(base_config, "load", lambda *a, **k: base)


def _replies(gh) -> list[str]:
    return [json.loads(stdin)["body"] for _, stdin in gh.calls if is_reply(stdin)]


def test_update_suggests_fix_and_waits(fix_env) -> None:
    assert main.handle(_update(), fix_env["deps"]) == "passed"
    body = bodies(fix_env["gh"])[-1]
    assert "**규칙:** ⚠️ 위반 2건 (lint 2) · 자동 수정 가능: `/fix lint`" in body
    assert fix_env["state"]["fix_calls"] == 0  # 승인 전에는 고치지 않는다
    from app.worker import update

    target = PRJob.model_validate_json(_update())
    assert update.has_pending_fix(target, fix_env["deps"])


def test_fix_command_resumes_and_reports(fix_env) -> None:
    main.handle(_update(), fix_env["deps"])
    assert main.handle(_fix_command(), fix_env["deps"]) == "fix:ready"
    assert fix_env["state"]["fix_calls"] == 1
    body = bodies(fix_env["gh"])[-1]
    assert "**lint 수정안:** 위반 1건을 고쳤습니다. 고친 뒤 테스트: 통과 (9개)." in body
    assert "자동으로 고치지 못한 위반 1건은 그대로 남습니다." in body
    assert "-import sys" in body
    assert "자동 수정 가능" not in body
    patches = list((fix_env["deps"].settings.data_dir / "fixes").rglob("*-lint.patch"))
    assert [p.read_text() for p in patches] == ["-import sys\n"]


def test_second_fix_command_has_nothing_pending(fix_env) -> None:
    main.handle(_update(), fix_env["deps"])
    main.handle(_fix_command(77), fix_env["deps"])
    assert main.handle(_fix_command(78), fix_env["deps"]) == "no_pending_fix"
    assert "승인을 기다리는 lint 수정이 없습니다" in _replies(fix_env["gh"])[-1]
    assert fix_env["state"]["fix_calls"] == 1


def test_fix_without_waiting_check_replies(fix_env) -> None:
    assert main.handle(_fix_command(), fix_env["deps"]) == "no_pending_fix"
    assert fix_env["deps"].store.command_handled(77)


def test_new_commit_invalidates_pending_fix(fix_env) -> None:
    main.handle(_update(), fix_env["deps"])
    fix_env["gh"].head = "c" * 40  # 승인 전에 새 커밋
    assert main.handle(_fix_command(), fix_env["deps"]) == "no_pending_fix"
    assert fix_env["state"]["fix_calls"] == 0


def test_recheck_starts_a_fresh_checkpoint(fix_env) -> None:
    main.handle(_update(), fix_env["deps"])
    main.handle(_fix_command(77), fix_env["deps"])
    main.handle(_fix_command(80, "/recheck"), fix_env["deps"])  # 같은 커밋을 다시 검사
    assert main.handle(_fix_command(81), fix_env["deps"]) == "fix:ready"  # 다시 기다리고 있었다


def test_no_approval_fixes_right_away(fix_env) -> None:
    _with_approval(fix_env, required=False)
    assert main.handle(_update(), fix_env["deps"]) == "passed"
    assert fix_env["state"]["fix_calls"] == 1
    assert "**lint 수정안:** 위반 1건을 고쳤습니다" in bodies(fix_env["gh"])[-1]


def test_failing_tests_block_suggestion(fix_env) -> None:
    failed = PytestRun(exit_code=1, summary=ReportSummary(passed=8, failed=1, total=9))
    use_runner(fix_env, lambda *a, **k: failed)
    main.handle(_update(), fix_env["deps"])
    body = bodies(fix_env["gh"])[-1]
    assert "자동 수정 가능" not in body
    assert "테스트가 통과하지 않아 자동 수정을 제안하지 않습니다 (실패 (9개 중 1개))" in body
    assert main.handle(_fix_command(), fix_env["deps"]) == "no_pending_fix"


@pytest.mark.parametrize(
    ("setup", "status", "text"),
    [
        (lambda s: s.update(after=(UNFIXABLE, Finding("a.py", 3, "error", "new", "lint", "x",
                                                      "F841"))),
         "new_violations", "고친 뒤 원래 없던 위반이 생김"),
        (lambda s: s.update(status=" M a.py\0?? tests/conftest.py\0"),
         "out_of_scope", "허용 범위 밖의 파일이 바뀜 (?? tests/conftest.py)"),
    ],
    ids=["new-violation", "new-file"],
)  # fmt: skip
def test_bad_fix_is_discarded(fix_env, setup, status: str, text: str) -> None:
    setup(fix_env["state"])
    main.handle(_update(), fix_env["deps"])
    assert main.handle(_fix_command(), fix_env["deps"]) == f"fix:{status}"
    body = bodies(fix_env["gh"])[-1]
    assert text in body
    assert not list((fix_env["deps"].settings.data_dir / "fixes").rglob("*.patch"))


def test_tests_changed_after_fix_is_discarded(fix_env) -> None:
    runs = iter([PASSED, PytestRun(exit_code=1, summary=ReportSummary(failed=1, total=9))])
    use_runner(fix_env, lambda *a, **k: next(runs))
    main.handle(_update(), fix_env["deps"])
    assert main.handle(_fix_command(), fix_env["deps"]) == "fix:tests_changed"
    assert "고친 뒤 테스트 결과가 달라짐" in bodies(fix_env["gh"])[-1]


def test_nothing_applied(fix_env) -> None:
    fix_env["monkeypatch"].setattr(
        lint, "fix_lint",
        lambda *a, **k: LintFix(SkillResult("lint", (FIXABLE,), "x"), applied=0, touched_files=()),
    )  # fmt: skip
    main.handle(_update(), fix_env["deps"])
    assert main.handle(_fix_command(), fix_env["deps"]) == "fix:nothing_fixed"


@pytest.mark.parametrize("skill", ["static", "structure", ""])
def test_other_fix_skills_are_not_supported(fix_env, skill: str) -> None:
    text = f"/fix {skill}".strip()
    assert main.handle(_fix_command(text=text), fix_env["deps"]) == "fix_unsupported"
    assert f"`{text}`는 아직 지원하지 않습니다" in _replies(fix_env["gh"])[-1]


def test_failed_fix_can_be_retried(fix_env) -> None:
    """수정 도중 환경 오류로 실패해도 체크포인트가 fix에 머물러 다음 시도가 이어 간다."""
    from app.sandbox.docker_cli import SandboxError
    from app.worker import retry

    main.handle(_update(), fix_env["deps"])
    calls = {"n": 0}

    def flaky_fix(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise SandboxError("docker 중단")
        return LintFix(SkillResult("lint", (UNFIXABLE,), "x"), applied=1, touched_files=("a.py",))

    fix_env["monkeypatch"].setattr(lint, "fix_lint", flaky_fix)
    fix_env["monkeypatch"].setattr(retry, "attempt", lambda: (1, False))
    with pytest.raises(SandboxError):
        main.handle(_fix_command(), fix_env["deps"])
    assert not fix_env["deps"].store.command_handled(77)
    assert main.handle(_fix_command(), fix_env["deps"]) == "fix:ready"  # RQ 재시도
