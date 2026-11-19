"""워커 흐름 테스트의 공용 값과 도우미 (test_main, test_command)."""

import json
from typing import Final

from app.models.repo_config import FixSettings, RepoConfig
from app.models.test_run import PytestRun, ReportSummary
from app.models.tools import RuffSetup
from app.worker import graph
from app.worker.base_config import BaseConfig

from .test_github import FakeGh

HEAD: Final = "a" * 40
PASSED: Final = PytestRun(exit_code=0, summary=ReportSummary(passed=9, total=9))
# 흐름 테스트는 승인 대기(/fix)를 단계별로 보려고 require_approval: true로 둔다.
# 기본값(자동 수정)의 흐름은 test_fix_flow·test_auto_flow가 본다
APPROVAL_REPO: Final = RepoConfig(fix=FixSettings(require_approval=True))
# ruff 설정이 없는 base. 도구 설정의 규칙이 없다
BASE: Final = BaseConfig(repo=APPROVAL_REPO, memory_gb=2, max_memory_gb=4, ruff=RuffSetup("0.17.5"))


def use_runner(env, fn) -> None:
    env["monkeypatch"].setattr(graph, "run_pr_tests", fn)


def bodies(gh: FakeGh) -> list[str]:
    return [body for args, stdin in gh.calls if stdin and (body := json.loads(stdin).get("body"))]
