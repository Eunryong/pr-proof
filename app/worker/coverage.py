"""안전망 입력 읽기와 참고 지표 diff-cover (docs/DESIGN.md F8).

`.pipeline-out/` 아래 파일(coverage.xml, changes.diff)과 worktree의 소스는 샌드박스 안의 PR
코드가 바꿀 수 있다. 호스트에서 읽을 때 심볼릭 링크·FIFO·장치 파일을 따라가지 않고, worktree
밖을 가리키는 경로와 큰 파일은 읽지 않는다. 판정 규칙은 `app/models/coverage.py`에 있다.
"""

import json
import os
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.models.coverage import (
    MAX_COVERAGE_XML_BYTES,
    CoverageData,
    mark_covered,
    parse_coverage_xml,
)
from app.models.finding import Finding

DIFF_COVER_TIMEOUT: Final = 60
MAX_SOURCE_BYTES: Final = 2 * 1024 * 1024
MAX_DIFF_BYTES: Final = 20 * 1024 * 1024
_ENV_PASSTHROUGH: Final = ("PATH",)
# diff-cover를 격리 모드(-I)로 실행한다. 작업 폴더·PYTHONPATH·사용자 site의 모듈을 import하지 않는다
_DIFF_COVER_MAIN: Final = (
    "import sys; from diff_cover.diff_cover_tool import main; sys.exit(main())"
)


def read_regular_file(path: Path, *, max_bytes: int) -> bytes | None:
    """일반 파일만 max_bytes까지 읽는다. 링크·FIFO·장치·너무 큰 파일·읽기 실패는 None.

    O_NOFOLLOW로 마지막 경로의 심볼릭 링크를 거부하고, O_NONBLOCK으로 FIFO에서 멈추지 않는다.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            return None
        with os.fdopen(fd, "rb", closefd=False) as f:
            data = f.read(max_bytes + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    return data if len(data) <= max_bytes else None


def read_coverage(
    coverage_xml: Path | None, *, max_bytes: int = MAX_COVERAGE_XML_BYTES
) -> CoverageData | None:
    """샌드박스가 남긴 coverage.xml을 읽는다. 없거나 깨졌으면 None (모름)."""
    if coverage_xml is None:
        return None
    data = read_regular_file(coverage_xml, max_bytes=max_bytes)
    if data is None:
        return None
    return parse_coverage_xml(data, max_bytes=max_bytes)


def _inside(worktree: Path, rel: str) -> Path | None:
    """worktree 안의 실제 경로. 절대 경로이거나 중간 링크가 worktree 밖을 가리키면 None."""
    if os.path.isabs(rel):
        return None
    root = worktree.resolve()
    path = (root / rel).resolve()
    return path if path.is_relative_to(root) else None


def read_sources(
    worktree: Path, files: Iterable[str], *, max_bytes: int = MAX_SOURCE_BYTES
) -> dict[str, str]:
    """위반이 있는 파일의 소스를 {리포 기준 경로: 내용}으로 읽는다. 못 읽은 파일은 빠진다."""
    sources: dict[str, str] = {}
    for rel in dict.fromkeys(files):
        path = _inside(worktree, rel)
        if path is None:
            continue
        data = read_regular_file(path, max_bytes=max_bytes)
        if data is None:
            continue
        try:
            sources[rel] = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return sources


def annotate_coverage(
    findings: Iterable[Finding], coverage_xml: Path | None, worktree: Path
) -> tuple[Finding, ...]:
    """위반에 covered를 채운다. coverage.xml은 `PytestRun.coverage_xml`(수정 전 실행)이다."""
    findings = tuple(findings)
    coverage = read_coverage(coverage_xml)
    if coverage is None:
        return mark_covered(findings, None)
    return mark_covered(findings, coverage, read_sources(worktree, (f.file for f in findings)))


@dataclass(frozen=True)
class DiffCoverage:
    """PR이 바꾼 줄의 커버리지 (참고 지표). 테스트가 import한 파일의 실행 단위 줄만 센다."""

    percent: float  # 소수 둘째 자리까지
    measured_lines: int  # 바뀐 줄 중 coverage가 센 줄 (빈 줄·주석·문서 파일·안 import된 파일 제외)
    uncovered_lines: int  # 그중 실행되지 않은 줄


def _minimal_env(home: Path) -> dict[str, str]:
    """diff-cover가 부르는 git(`rev-parse --show-toplevel`)이 사용자 설정을 읽지 않게 한다."""
    env = {k: os.environ[k] for k in _ENV_PASSTHROUGH if k in os.environ}
    env |= {
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C.UTF-8",
    }
    return env


def diff_coverage(
    coverage_xml: Path | None,
    changes_diff: Path,
    *,
    timeout: float = DIFF_COVER_TIMEOUT,
    python: str = sys.executable,
) -> DiffCoverage | None:
    """호스트에서 diff-cover로 변경 줄 커버리지를 잰다. 잴 수 없으면 None.

    diff-cover는 XML과 diff만 읽고 PR 코드를 실행하지 않는다. 그래도 입력은 믿지 않는다.
    - coverage.xml은 먼저 안전한 파서로 검사해 통과한 바이트만 넘긴다 (diff-cover는 표준
      ElementTree로 읽으므로 DTD·엔티티가 있으면 넘기지 않는다).
    - 입력은 임시 폴더에 복사해 그 안에서 실행한다. diff-cover가 부르는 `git rev-parse`는 그
      임시 폴더의 빈 저장소를 보고, PR worktree나 사용자 git 설정을 읽지 않는다.
    - 최소 환경, 격리 모드(-I), 시간 제한.
    바뀐 줄 중 coverage가 센 줄이 하나도 없으면 비율이 의미가 없어 None이다.
    """
    if coverage_xml is None:
        return None
    xml = read_regular_file(coverage_xml, max_bytes=MAX_COVERAGE_XML_BYTES)
    diff = read_regular_file(changes_diff, max_bytes=MAX_DIFF_BYTES)
    if xml is None or diff is None or parse_coverage_xml(xml) is None:
        return None
    with tempfile.TemporaryDirectory(prefix="pr-proof-diffcover-") as tmp:
        work = Path(tmp).resolve()
        (work / "coverage.xml").write_bytes(xml)
        (work / "changes.diff").write_bytes(diff)
        env = _minimal_env(work)
        report = work / "report.json"
        try:
            subprocess.run(
                ["git", "init", "--quiet", str(work)],
                cwd=work,
                env=env,
                capture_output=True,
                timeout=timeout,
                check=True,
            )
            proc = subprocess.run(
                [
                    python, "-I", "-c", _DIFF_COVER_MAIN,
                    "coverage.xml", "--diff-file", "changes.diff",
                    "--format", f"json:{report.name}", "--quiet", "--total-percent-float",
                ],
                cwd=work,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )  # fmt: skip
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        return _parse_report(report)


def _parse_report(report: Path) -> DiffCoverage | None:
    try:
        data = json.loads(report.read_text())
        measured = int(data["total_num_lines"])
        violations = int(data["total_num_violations"])
        percent = float(data["total_percent_covered"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if measured <= 0 or not 0 <= violations <= measured:
        return None
    return DiffCoverage(percent=percent, measured_lines=measured, uncovered_lines=violations)
