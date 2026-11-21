"""실제 claude CLI로 확인한다: 수정 작성 호출에 파이프라인 도구 둘이 붙고, 부르면 결과가 돌아온다
(docs/DESIGN.md 4.7, 8.4, 위협 표 T25).

모델을 실제로 부르므로 `PR_PROOF_REAL_AI=1`일 때만 돈다. 도구의 구현은 정해진 글을 돌려주는 함수로
바꾼다(샌드박스 없이). 파이프라인이 쓰는 경로 그대로 모델 폴더, 경로 감시 훅, `--restricted`,
`--strict-mcp-config`, stdio MCP 서버(`mcp_relay.py`)가 한 호출에서 같이 돈다.
"""

import json
import os
import subprocess
from pathlib import Path
from typing import Final

import pytest

from worker import mcp_pure, repo
from worker.ai import claude_cli, workdir

REAL_AI_ENV: Final = "PR_PROOF_REAL_AI"
MARK: Final = "PROOF-TOOL-RESULT-71c2"
PROMPT: Final = (
    "Call the tool mcp__proof__check_rules once, then call mcp__proof__run_tests once with "
    'code "before". Then edit src/app.py with the Edit tool so that it reads `X = 2`. '
    "Finally print the exact text both tools returned."
)

pytestmark = pytest.mark.skipif(
    os.environ.get(REAL_AI_ENV) != "1", reason=f"{REAL_AI_ENV}=1일 때만 실제 모델을 부른다"
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd, check=True, capture_output=True,
    )  # fmt: skip


def test_pipeline_tools_are_listed_called_and_answered(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    (wt / "src").mkdir(parents=True)
    _git(wt, "init", "-q", "-b", "main")
    repo.record_git_dir(wt)
    (wt / "src" / "app.py").write_text("X = 1\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "init")
    seen: list[tuple[str, dict, str]] = []
    captured: list[str] = []

    def handler(name: str):
        def run(args):
            # 도구가 돌 때 worktree가 모델 폴더의 그때 상태와 같다
            seen.append((name, dict(args), (wt / "src" / "app.py").read_text()))
            return f"{MARK} {name}"

        return run

    def runner(*args):
        proc = claude_cli._run(*args)
        captured.append(proc.stdout)
        return proc

    result = workdir.run_in_model_folder(
        wt, PROMPT, model="haiku", max_budget_usd=0.5, runner=runner,
        timeout=claude_cli.FIX_TOOLS_TIMEOUT_S,
        system_prompt="너는 파이프라인의 시험 단계다. 사용자 메시지가 시키는 도구 호출을 한다.",
        tools={mcp_pure.CHECK_RULES: handler("check"), mcp_pure.RUN_TESTS: handler("tests")},
    )  # fmt: skip

    init = next(json.loads(line) for line in captured[0].splitlines() if '"init"' in line)
    assert set(mcp_pure.TOOL_NAMES) <= set(init["tools"]), init["tools"]
    assert result.error is None, result.detail
    assert [(n, a) for n, a, _ in seen] == [("check", {}), ("tests", {"code": "before"})]
    assert result.tool_usage.check_rules == 1 and result.tool_usage.run_tests == 1
    assert MARK in result.text
    assert (wt / "src" / "app.py").read_text() == "X = 2\n"
    assert result.unexpected_tools == ()
