"""워커 흐름 테스트의 공용 값과 도우미 (test_main, test_command)."""

import json
from typing import Final

from app.models.finding import Finding
from app.models.repo_config import RepoConfig
from app.models.test_run import PytestRun, ReportSummary
from app.worker import graph
from app.worker.base_config import BaseConfig

from .test_github import FakeGh

HEAD: Final = "a" * 40
PASSED: Final = PytestRun(exit_code=0, summary=ReportSummary(passed=9, total=9))
BASE: Final = BaseConfig(
    repo=RepoConfig(), memory_gb=2, max_memory_gb=4, ruff_config=None, ruff_source="기본 규칙"
)
VIOLATION: Final = Finding(
    file="a.py", line=1, severity="error", message="m", skill="lint", source="x", code="F401"
)


def use_runner(env, fn) -> None:
    env["monkeypatch"].setattr(graph, "run_pr_tests", fn)


def bodies(gh: FakeGh) -> list[str]:
    return [json.loads(stdin)["body"] for args, stdin in gh.calls if stdin]
