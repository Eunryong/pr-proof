"""PR 테스트 실행 컨트롤러: 서비스를 순서대로 불러 한 PR의 테스트를 돌린다.

LangGraph의 "테스트" 노드와 수동 명령(/recheck)이 이 함수를 부른다.
결과를 코멘트로 바꾸는 일은 views.test_report가 한다.
"""

from collections.abc import Callable
from pathlib import Path

from app.models.test_run import PytestRun
from app.services import sandbox


def run_pr_tests(
    worktree: Path,
    *,
    job_id: str | None = None,
    is_stale: Callable[[], bool] | None = None,
    venv_cache: Path = sandbox.VENV_CACHE,
) -> PytestRun:
    """이미지 준비 → .venv 준비 → 작업 컨테이너에서 pytest → 결과.

    Docker·설치 실패는 sandbox.SandboxError로 올라간다 (환경 오류, PR에 ❌를 달지 않는다).
    """
    image = sandbox.ensure_image()
    venv = sandbox.ensure_venv(worktree, image, cache_dir=venv_cache)
    with sandbox.sandbox(worktree, venv, job_id=job_id) as container:
        return sandbox.run_tests(container, worktree, is_stale=is_stale)
