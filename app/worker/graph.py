"""워커의 작업 흐름. 지금은 테스트 단계 하나이고, 1단계에서 LangGraph 직선 그래프
(체크아웃 → 테스트 → 코멘트)로 묶는다.
"""

from collections.abc import Callable
from pathlib import Path

from app.models.test_run import PytestRun
from app.sandbox import container, docker_cli, pytest_runner, venv


def run_pr_tests(
    worktree: Path,
    *,
    job_id: str | None = None,
    is_stale: Callable[[], bool] | None = None,
    venv_cache: Path = venv.VENV_CACHE,
) -> PytestRun:
    """테스트 단계: 이미지 준비 → .venv 준비 → 작업 컨테이너에서 pytest → 결과.

    Docker·설치 실패는 docker_cli.SandboxError로 올라간다 (환경 오류, PR에 ❌를 달지 않는다).
    """
    image = docker_cli.ensure_image()
    venv_dir = venv.ensure_venv(worktree, image, cache_dir=venv_cache)
    with container.running(worktree, venv_dir, job_id=job_id) as name:
        return pytest_runner.run_tests(name, worktree, is_stale=is_stale)
