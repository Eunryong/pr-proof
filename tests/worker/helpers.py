"""워커 흐름 테스트의 공용 값과 도우미 (test_main, test_command)."""

import json
from typing import Final

from app.worker import graph
from app.worker.base_config import BaseConfig
from app.worker.core.tools_pure import RuffSetup
from app.worker.repo_config_pure import FixSettings, PrDescriptionSettings, RepoConfig
from app.worker.testing.test_run_pure import PytestRun, ReportSummary

from .test_github import FakeGh

HEAD: Final = "a" * 40
PASSED: Final = PytestRun(exit_code=0, summary=ReportSummary(passed=9, total=9))
# 흐름 테스트는 승인 대기(/fix)를 단계별로 보려고 require_approval: true로 둔다.
# 기본값(자동 수정)의 흐름은 test_fix_flow·test_auto_flow가 본다. PR 설명 확인(4.5 끝)은 꺼서 가짜
# 모델의 차례에 끼어들지 않게 한다 (그 확인은 test_pr_description*이 본다)
APPROVAL_REPO: Final = RepoConfig(
    fix=FixSettings(require_approval=True), pr_description=PrDescriptionSettings(enabled=False)
)
# ruff 설정이 없는 base. 도구 설정의 규칙이 없다
BASE: Final = BaseConfig(repo=APPROVAL_REPO, memory_gb=2, max_memory_gb=4, ruff=RuffSetup("0.17.5"))


def use_runner(env, fn) -> None:
    env["monkeypatch"].setattr(graph, "run_pr_tests", fn)


def bodies(gh: FakeGh) -> list[str]:
    return [body for args, stdin in gh.calls if stdin and (body := json.loads(stdin).get("body"))]
