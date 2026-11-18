"""3단계 평가 케이스 E2E (eval/static/*.yaml).

lint 케이스와 같은 방식이다: 로컬 git 원격 + 실제 ruff + 실제 샌드박스 테스트, GitHub만 가짜.
AI(claude)는 기본으로 케이스의 `ai` 편집을 적용하는 가짜다. 판정·검증은 모두 진짜다.
`PR_PROOF_REAL_AI=1`이면 로그인된 claude를 실제로 부른다 (비용이 든다. 6단계 평가 세트용).
"""

import os
from pathlib import Path
from typing import Final

import fakeredis
import pytest
import yaml

from app import config
from app.models import state_keys
from app.models.job import PRJob
from app.worker import main, repo
from app.worker.ai import claude_cli
from app.worker.ai.claude_cli import ClaudeResult
from app.worker.deps import Deps
from app.worker.github import GitHub
from app.worker.state import Store
from tests.sandbox_names import TEST_NAMES

from ..worker.test_github import FakeGh
from .test_lint_cases import _bodies, _git, _remote, shared_venvs  # noqa: F401

CASES_DIR: Final = Path(__file__).resolve().parents[2] / "eval" / "static"
CASES: Final = sorted(CASES_DIR.glob("*.yaml"))
REPO: Final = "o/r"
BRANCH: Final = "pipeline/pr-1-fix-static"
REAL_AI_ENV: Final = "PR_PROOF_REAL_AI"


def _finding_codes(body: str) -> list[str]:
    """코멘트의 "<스킬> 위반:" 목록의 규칙 코드. 자동 수정 대상이 아닌 위반 목록은 세지 않는다."""
    codes, listing = [], False
    for line in body.splitlines():
        if line.endswith(" 위반:") and not line.startswith("자동 수정"):
            listing = True
        elif not line.startswith("- `"):
            listing = False
        elif listing and "` `" in line:
            codes.append(line.split("` `")[1].split("`")[0])
    return sorted(codes)


def _scripted_ai(steps: list[list[dict]]):
    """회차마다 steps의 다음 편집을 worktree에 적용하는 가짜 run_claude.

    편집이 회차보다 적으면 마지막 것을 다시 쓰고, find가 이미 없으면 그 편집은 건너뛴다.
    """
    calls = {"n": 0}

    def run(worktree: Path, prompt: str, *, model: str, max_budget_usd: float, **kw):
        edits = steps[min(calls["n"], len(steps) - 1)]
        calls["n"] += 1
        for edit in edits:
            path = worktree / edit["file"]
            text = path.read_text()
            if edit["find"] in text:
                path.write_text(text.replace(edit["find"], edit["replace"], 1))
        return ClaudeResult(ok=True, error=None, cost_usd=0.001, num_turns=1,
                            permission_denials=(), text="케이스의 편집을 적용했습니다")  # fmt: skip

    return run


@pytest.mark.docker
@pytest.mark.parametrize("case_path", CASES, ids=[p.stem for p in CASES])
def test_static_case(case_path: Path, eval_repo: Path, shared_venvs: Path, tmp_path: Path,  # noqa: F811
                     monkeypatch) -> None:  # fmt: skip
    case = yaml.safe_load(case_path.read_text())
    expect = case["expect"]
    real_ai = os.environ.get(REAL_AI_ENV) == "1"
    if real_ai and expect.get("ai_only_scripted"):
        pytest.skip("가짜 AI로만 확인하는 케이스")
    if not real_ai:
        monkeypatch.setattr(claude_cli, "run_claude", _scripted_ai(case.get("ai") or [[]]))
    remote = _remote(tmp_path, eval_repo, case)
    monkeypatch.setattr(repo, "remote_url", lambda name: str(remote["path"]))
    monkeypatch.setenv(config.CONFIG_DIR_ENV, str(tmp_path / "config"))
    data = tmp_path / "data"
    data.mkdir()
    (data / "venvs").symlink_to(shared_venvs, target_is_directory=True)
    redis = fakeredis.FakeStrictRedis()
    gh = FakeGh(head=remote["head"], base=remote["base"])
    deps = Deps(
        store=Store(data / "pipeline.db", redis),
        github=GitHub(gh),
        settings=config.PipelineConfig(
            repos=[REPO],
            data_dir=data,
            sandbox=config.SandboxConfig(
                image=TEST_NAMES.image, container_prefix=TEST_NAMES.container_prefix
            ),
        ),
    )
    update = PRJob(
        repo=REPO, pr_number=1, kind="update", head_sha=remote["head"], base_sha=remote["base"],
        source="webhook",
    )  # fmt: skip
    redis.set(state_keys.latest_head(REPO, 1), remote["head"])

    status = main.handle(update.model_dump_json(), deps)
    body = _bodies(gh)[-1]
    rules = next(line for line in body.splitlines() if line.startswith("**규칙:**"))
    assert _finding_codes(body) == sorted(expect["findings"]), body
    assert "`/fix lint`" not in rules, "케이스가 lint 위반을 넣지 않아야 static만 본다"

    match expect["proposal"]:
        case "approval":
            assert status == "passed"
            assert rules.endswith("자동 수정 가능: `/fix static`"), rules
        case "uncovered":
            assert status == "passed" and "`/fix static`" not in rules
            assert "테스트가 지나가지 않아 자동 수정 대상 아님" in body
        case "tests":
            assert status == "failed" and "`/fix static`" not in rules
            assert "static 자동 수정을 제안하지 않습니다 (테스트가 실패함)" in body
    if expect["proposal"] != "approval":
        assert _git(remote["path"], "branch", "--list", BRANCH) == ""
        return

    fix = PRJob(repo=REPO, pr_number=1, kind="command", command="/fix static", comment_id=7,
                source="webhook")  # fmt: skip
    assert main.handle(fix.model_dump_json(), deps) == f"fix:{expect['fix']}"
    fixed_body = _bodies(gh)[-1]
    assert "AI 호출" in fixed_body, fixed_body
    if expect["fix"] != "ready":
        assert gh.pulls == {} and _git(remote["path"], "branch", "--list", BRANCH) == ""
        return

    ((number, pull),) = gh.pulls.items()
    assert f"제안 PR #{number}을 열었습니다." in fixed_body, fixed_body
    assert (pull["base"], pull["head"]) == ("feature", BRANCH)
    pushed = _git(remote["path"], "rev-parse", BRANCH)
    assert _git(remote["path"], "rev-parse", f"{pushed}^") == remote["head"]
    changed = _git(remote["path"], "diff", "--name-only", remote["head"], pushed).split()
    assert changed == [expect["file"]]
    if expect.get("same_as_base") and not real_ai:
        text = _git(remote["path"], "show", f"{pushed}:{expect['file']}")
        assert text == _git(remote["path"], "show", f"{remote['base']}:{expect['file']}")
