"""샌드박스 안에서 pytest를 실행하고 결과 JSON과 coverage.xml을 남긴다 (docs/DESIGN.md F5·F6)."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Final

from app.models.test_run import PytestRun, ReportSummary
from app.sandbox import docker_cli
from app.sandbox.container import (
    DEFAULT_TIMEOUT,
    OUT_DIR,
    PIPELINE_DIR,
    POLL_SECONDS,
    ExecResult,
    run_in_sandbox,
)

REPORT_FILE: Final = f"{OUT_DIR}/report.json"
COVERAGE_XML: Final = f"{OUT_DIR}/coverage.xml"
# coverage.xml 경로를 리포 기준 상대 경로로 남기는 파이프라인 쪽 설정 (docker/sandbox/coveragerc)
COVERAGERC: Final = f"{PIPELINE_DIR}/coveragerc"
MAX_OUTPUT_CHARS: Final = 4000
# docker exec 자체가 실패한 종료 코드: 125 docker 오류, 126 실행 불가, 127 명령 없음(.venv 손상)
DOCKER_EXEC_FAILED: Final = frozenset({125, 126, 127})


def pythonpath_for(worktree: Path) -> str:
    """리포를 설치하지 않으므로 PYTHONPATH로 import 경로를 준다. src 구조면 /work/src도 넣는다."""
    return "/work/src:/work" if (worktree / "src").is_dir() else "/work"


def pytest_argv() -> list[str]:
    return [
        ".venv/bin/pytest",
        "-p", "no:cacheprovider",
        "--json-report", f"--json-report-file={REPORT_FILE}",
        "--cov", f"--cov-config={COVERAGERC}",
        f"--cov-report=xml:{COVERAGE_XML}",
    ]  # fmt: skip


def read_summary(report: Path) -> ReportSummary | None:
    """report.json을 읽는다. 없거나 깨졌으면 None."""
    if not report.is_file():
        return None
    try:
        return ReportSummary.from_report(json.loads(report.read_text()))
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
        return None


def run_tests(
    container: str,
    worktree: Path,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    is_stale: Callable[[], bool] | None = None,
    poll_seconds: float = POLL_SECONDS,
) -> PytestRun:
    """컨테이너 안에서 pytest를 실행하고 결과를 PytestRun으로 돌려준다."""
    report = worktree / REPORT_FILE
    coverage_xml = worktree / COVERAGE_XML
    for leftover in (report, coverage_xml, worktree / OUT_DIR / ".coverage"):
        leftover.unlink(missing_ok=True)

    result = run_in_sandbox(
        container,
        pytest_argv(),
        env={"PYTHONPATH": pythonpath_for(worktree), "PYTHONDONTWRITEBYTECODE": "1"},
        timeout=timeout,
        is_stale=is_stale,
        poll_seconds=poll_seconds,
    )
    summary = read_summary(report)
    _raise_if_environment_failed(container, result, has_report=summary is not None)
    return PytestRun(
        exit_code=result.returncode,
        summary=summary,
        report_path=report if summary is not None else None,
        coverage_xml=coverage_xml if coverage_xml.is_file() else None,
        timed_out=result.timed_out,
        cancelled=result.cancelled,
        output=(result.stdout + result.stderr)[-MAX_OUTPUT_CHARS:],
    )


def _raise_if_environment_failed(container: str, result: ExecResult, *, has_report: bool) -> None:
    """PR 코드 탓이 아닌 실패를 SandboxError(환경 오류)로 올린다.

    docker exec 자체가 실패했거나, 결과 파일이 없는데 컨테이너가 이미 죽어 있으면 환경 오류다.
    컨테이너가 살아 있는데 결과가 없으면 PR 코드 문제(unrunnable)로 둔다.
    """
    returncode = result.returncode
    if result.cancelled or result.timed_out or returncode is None:
        return
    if returncode in DOCKER_EXEC_FAILED:
        raise docker_cli.SandboxError(
            f"docker exec 실패 ({returncode}): {result.stderr.strip()[-500:]}"
        )
    if not has_report and returncode != 0 and not docker_cli.container_running(container):
        raise docker_cli.SandboxError(f"컨테이너 {container}가 실행 중이 아니다 ({returncode})")
