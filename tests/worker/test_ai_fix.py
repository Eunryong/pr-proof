"""worker/ai_fix.py: AI 수정 루프.

가짜 AI가 임시 git 리포의 파일을 바꾸고, 범위 검사는 실제 git으로 한다.

규칙 재검사와 테스트는 가짜다 (실제 ruff·pytest는 docker 테스트가 본다).
"""

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Final

import pytest

from app.models.finding import Finding, SkillResult
from app.models.repo_config import FixSettings, ModelSettings, RepoConfig
from app.models.test_run import PytestRun, ReportSummary
from app.worker import repo
from app.worker.ai.claude_cli import AIErrorKind, ClaudeResult, PermissionDenial
from app.worker.ai_fix import AIEnvironmentError, CodeFixTools, code_fix, module_constants

MAGIC: Final = Finding(
    "src/app.py", 2, "warning", "Magic value `3`", "static", "기본 규칙", "PLR2004"
)
PASSED: Final = PytestRun(exit_code=0, summary=ReportSummary(passed=4, total=4))
FAILED: Final = PytestRun(
    exit_code=1,
    summary=ReportSummary(passed=3, failed=1, total=4, failed_tests=("tests/test_app.py::t",)),
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip


@pytest.fixture
def wt(tmp_path: Path) -> Path:
    path = tmp_path / "wt"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    repo.record_git_dir(path)
    files = {
        ".gitignore": "build/\n",
        "src/app.py": "LIMIT = 10\ndef f(x):\n    return x > 3\n",
        "src/other.py": "Y = 1\n",
        "tests/test_app.py": "def test_x():\n    assert True\n",
    }
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "base")
    (path / "build").mkdir()
    (path / "build" / "old.txt").write_text("cache")  # 이미 있던 무시된 파일
    return path


def _base(wt: Path) -> str:
    return _git(wt, "rev-parse", "HEAD").strip()


def _ok(text: str = "상수로 뺐다", cost: float | None = 0.01) -> ClaudeResult:
    return ClaudeResult(ok=True, error=None, cost_usd=cost, num_turns=3, permission_denials=(),
                        text=text)  # fmt: skip


def _fix_file(wt: Path) -> None:
    (wt / "src/app.py").write_text(
        "LIMIT = 10\nTHRESHOLD = 3\ndef f(x):\n    return x > THRESHOLD\n"
    )


class Fake:
    """AI 호출마다 actions의 다음 동작을 실행하고, 재검사·테스트 결과를 차례로 돌려준다."""

    def __init__(self, wt: Path, actions: list[Callable[[Path], ClaudeResult]]):
        self.wt, self.actions = wt, list(actions)
        self.prompts: list[str] = []
        self.models: list[tuple[str, float]] = []
        self.rechecks: list[SkillResult] = []
        self.tests: list[PytestRun] = []
        self.test_calls = 0
        self.recorded: list[tuple[int, str, float]] = []

    def run_ai(self, worktree, prompt, *, model, max_budget_usd):
        self.prompts.append(prompt)
        self.models.append((model, max_budget_usd))
        return self.actions.pop(0)(worktree)

    def recheck(self, changed):
        return self.rechecks.pop(0) if self.rechecks else SkillResult("static")

    def run_tests(self):
        self.test_calls += 1
        return self.tests.pop(0) if self.tests else PASSED

    def tools(self, **kw) -> CodeFixTools:
        return CodeFixTools(
            run_ai=self.run_ai, recheck=self.recheck, recheck_lint=kw.get("recheck_lint"),
            run_tests=self.run_tests,
            record_call=lambda a, m, r, c: self.recorded.append((a, m, c)),
            is_stale=kw.get("is_stale", lambda: False),
        )  # fmt: skip


def _config(retries: int = 3, budget: float = 0.5) -> RepoConfig:
    return RepoConfig(
        fix=FixSettings(max_retries=retries),
        model=ModelSettings(budget_per_pr_usd=budget),
    )


def _run(wt: Path, fake: Fake, *, spent: float = 0.0, config: RepoConfig | None = None, **kw):
    return code_fix(
        wt, _base(wt), "static", SkillResult("static", (MAGIC,)), [MAGIC],
        lint_before=kw.pop("lint_before", None), test_before=PASSED,
        config=config or _config(), spent_usd=spent, tools=fake.tools(**kw),
    )  # fmt: skip


def _edit_ok(wt: Path) -> ClaudeResult:
    _fix_file(wt)
    return _ok()


def test_fix_passes_on_first_attempt(wt: Path) -> None:
    fake = Fake(wt, [_edit_ok])
    out = _run(wt, fake)
    assert out.status == "ready" and out.fixed == 1
    assert out.changed_files == ("src/app.py",) and out.explanation == "상수로 뺐다"
    assert (out.ai_calls, out.ai_cost_usd, out.models) == (1, 0.01, ("haiku",))
    assert fake.test_calls == 1 and fake.recorded == [(1, "haiku", 0.01)]
    assert "src/app.py" in fake.prompts[0] and "LIMIT" in fake.prompts[0]  # 기존 상수 예시


def test_remaining_violation_is_fed_back_and_retried(wt: Path) -> None:
    fake = Fake(wt, [lambda w: _ok(), _edit_ok])
    (wt / "src/app.py").write_text("LIMIT = 10\ndef f(x):\n    return x > 3  # 손댐\n")
    fake.rechecks = [SkillResult("static", (MAGIC,)), SkillResult("static")]
    out = _run(wt, fake)
    assert out.status == "ready" and out.ai_calls == 2
    assert "아직 남은 위반: src/app.py:2 PLR2004" in fake.prompts[1]
    assert fake.test_calls == 1  # 위반이 남은 회차에는 테스트를 돌리지 않는다


def test_new_lint_violation_is_a_failure(wt: Path) -> None:
    lint_before = SkillResult("lint", ())
    new_lint = Finding("src/app.py", 2, "error", "unused", "lint", "x", "F401")
    lints = [SkillResult("lint", (new_lint,)), SkillResult("lint", ())]
    fake = Fake(wt, [_edit_ok, _edit_ok])
    out = _run(wt, fake, lint_before=lint_before, recheck_lint=lambda changed: lints.pop(0))
    assert out.status == "ready" and out.ai_calls == 2
    assert "새로 생긴 lint 위반: src/app.py:2 F401 unused" in fake.prompts[1]


def test_failing_tests_are_fed_back(wt: Path) -> None:
    fake = Fake(wt, [_edit_ok, _edit_ok])
    fake.tests = [FAILED, PASSED]
    out = _run(wt, fake)
    assert out.status == "ready" and fake.test_calls == 2
    assert "실패한 테스트: tests/test_app.py::t" in fake.prompts[1]


def test_routing_light_light_heavy_then_gives_up(wt: Path) -> None:
    fake = Fake(wt, [lambda w: _ok()] * 3)  # 아무것도 바꾸지 않는다
    out = _run(wt, fake)
    assert out.status == "ai_failed" and out.detail == "AI가 아무 파일도 바꾸지 않음"
    assert [m for m, _ in fake.models] == ["haiku", "haiku", "sonnet"]
    assert fake.test_calls == 0


@pytest.mark.parametrize(
    ("write", "reason"),
    [
        (lambda w: (w / "src/other.py").write_text("Y = 2\n"), "허용 목록 밖"),
        (lambda w: (w / "tests/test_app.py").write_text("def test_x():\n    pass\n"), "테스트"),
        (lambda w: (w / "src/new.py").write_text(""), "허용 목록 밖"),
        # 무시된 경로에 새 conftest: git status에 안 보이지만 pytest가 읽는다
        (lambda w: (w / "build" / "conftest.py").write_text("import os\n"), "무시된"),
    ],
    ids=["other-file", "test-file", "new-file", "ignored-conftest"],
)
def test_out_of_scope_change_stops_before_running_anything(wt: Path, write, reason: str) -> None:
    def act(w: Path) -> ClaudeResult:
        _fix_file(w)
        write(w)
        return _ok()

    fake = Fake(wt, [act, _edit_ok])
    out = _run(wt, fake)
    assert out.status == "out_of_scope" and reason in out.detail
    assert fake.test_calls == 0 and out.ai_calls == 1  # 다시 시도하지 않는다


def test_existing_ignored_file_change_is_fine(wt: Path) -> None:
    def act(w: Path) -> ClaudeResult:
        _fix_file(w)
        (w / "build" / "old.txt").write_text("changed")
        return _ok()

    assert _run(wt, Fake(wt, [act])).status == "ready"


def test_budget_stops_the_loop(wt: Path) -> None:
    fake = Fake(wt, [lambda w: _ok(cost=None)] * 3)  # 비용을 모르면 호출당 상한을 쓴 것으로 본다
    out = _run(wt, fake, config=_config(budget=0.35))
    assert out.status == "budget" and out.ai_calls == 2
    assert [b for _, b in fake.models] == [pytest.approx(0.30), pytest.approx(0.05)]
    assert "PR당 예산 $0.35" in out.detail


def test_spent_budget_from_earlier_runs_counts(wt: Path) -> None:
    fake = Fake(wt, [_edit_ok])
    out = _run(wt, fake, spent=0.5)
    assert out.status == "budget" and out.ai_calls == 0 and fake.prompts == []


@pytest.mark.parametrize(
    "result",
    [
        ClaudeResult(ok=False, error=AIErrorKind.LAUNCH, cost_usd=None, num_turns=0,
                     permission_denials=(), text="", detail="claude: not found"),
        ClaudeResult(ok=False, error=AIErrorKind.EXIT, cost_usd=0.0, num_turns=1,
                     permission_denials=(), text="Not logged in · Please run /login"),
    ],
    ids=["launch", "not-logged-in"],
)  # fmt: skip
def test_environment_problem_raises_for_retry(wt: Path, result: ClaudeResult) -> None:
    fake = Fake(wt, [lambda w: result])
    with pytest.raises(AIEnvironmentError):
        _run(wt, fake)
    assert len(fake.recorded) == 1  # 비용 기록은 남긴다


def test_timeout_counts_full_call_budget_and_retries(wt: Path) -> None:
    timeout = ClaudeResult(ok=False, error=AIErrorKind.TIMEOUT, cost_usd=None, num_turns=0,
                           permission_denials=(), text="")  # fmt: skip
    fake = Fake(wt, [lambda w: timeout, _edit_ok])
    out = _run(wt, fake)
    assert out.status == "ready" and out.ai_cost_usd == pytest.approx(0.31)


def test_stale_commit_stops(wt: Path) -> None:
    assert _run(wt, Fake(wt, [_edit_ok]), is_stale=lambda: True) is None


def test_permission_denials_are_logged(wt: Path, caplog) -> None:
    denied = (PermissionDenial("Read", {"file_path": "/etc/passwd"}),)

    def act2(w: Path) -> ClaudeResult:
        _fix_file(w)
        return ClaudeResult(ok=True, error=None, cost_usd=0.01, num_turns=1,
                            permission_denials=denied, text="ok")  # fmt: skip

    assert _run(wt, Fake(wt, [act2])).status == "ready"
    assert "허용 범위 밖 도구 호출" in caplog.text


def test_module_constants_lists_upper_names_only(wt: Path) -> None:
    (wt / "src/app.py").write_text("A: int = 1\nB = 2\nlower = 3\nC, D = 1, 2\n")
    (wt / "src/bad.py").write_text("def (:\n")
    assert module_constants(wt, ["src/app.py", "src/bad.py"]) == {"src/app.py": ["A", "B"]}


def test_symlink_pointing_outside_refuses_to_run_ai(wt: Path, tmp_path: Path) -> None:
    """PR이 넣은 링크 폴더를 거치면 AI의 쓰기가 worktree 밖으로 가고 git status에도 안 보인다."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (wt / "docs").symlink_to(outside, target_is_directory=True)
    (wt / "inner").symlink_to(wt / "src", target_is_directory=True)  # 안을 가리키면 괜찮다
    _git(wt, "add", ".")
    _git(wt, "commit", "-q", "-m", "pr adds links")
    fake = Fake(wt, [_edit_ok])
    out = _run(wt, fake)
    assert out.status == "refused" and "`docs`" in out.detail and "inner" not in out.detail
    assert fake.prompts == []  # AI를 부르지 않는다


def test_tampering_with_pipeline_config_is_out_of_scope(wt: Path) -> None:
    """`.pipeline-out/`은 범위 검사에서 빠진다. AI가 base의 ruff 설정을 고쳐 규칙을 끄지 못한다."""
    config = wt / ".pipeline-out" / "base" / "ruff.toml"
    config.parent.mkdir(parents=True)
    config.write_text("[lint]\nselect = ['E']\n")

    def act(w: Path) -> ClaudeResult:
        _fix_file(w)
        (w / ".pipeline-out" / "base" / "ruff.toml").write_text("[lint]\nignore = ['ALL']\n")
        return _ok()

    fake = Fake(wt, [act, _edit_ok])
    tools = fake.tools()
    tools.guarded = (".pipeline-out/base/ruff.toml",)
    out = code_fix(
        wt, _base(wt), "static", SkillResult("static", (MAGIC,)), [MAGIC], lint_before=None,
        test_before=PASSED, config=_config(), spent_usd=0.0, tools=tools,
    )  # fmt: skip
    assert out.status == "out_of_scope" and "ruff.toml" in out.detail
    assert fake.test_calls == 0 and out.ai_calls == 1
