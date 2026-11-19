"""Python 언어 프로필 (docs/DESIGN.md 2.3, 7.7).

언어마다 다른 것 중 Python의 것을 여기에 모은다: 의존성 설치와 캐시(.venv), 테스트 실행과 결과
읽기(pytest, report.json), 커버리지 읽기(coverage.xml), 도구(ruff, ruff-format).
3단계에서는 흩어져 있던 코드를 옮기기만 했다. 두 번째 언어를 붙일 때 그 차이를 보고 인터페이스를
정한다. 판단 규칙(coverage.xml과 문장 범위 읽기, 테스트 파일의 정의, ruff 설정과 버전 읽기, 도구
목록)은 웹·워커 공용의 `models/`(`models/coverage.py`, `models/scope.py`의 `is_test_file`,
`models/tools.py`)에 남는다. 그 규칙을 쓰는 판정(허용 범위, 도구 선택)이 models에 있고, 계층이
worker → sandbox → models라 models가 이 모듈을 부를 수 없기 때문이다.

.venv (4.5, 8.1): 의존성만 설치하고 대상 리포 자체는 설치하지 않는다. 설치는 네트워크가 열린
별도 컨테이너에서 wheel만 받고(`--only-binary :all:`, 소스 배포본은 PR이 고를 수 있는 빌드 코드를
실행한다), 호스트의 것은 마운트하지 않는다. 의존성 파일은 표준 입력으로 복사해 넣고, 붙이는 호스트
경로는 그 설치만의 빈 출력 폴더 하나다. 테스트 컨테이너에는 읽기 전용으로 붙인다.

coverage.xml과 worktree의 소스는 샌드박스 안의 PR 코드가 바꿀 수 있다. 호스트에서 읽을 때 심볼릭
링크·FIFO·장치 파일을 따라가지 않고, worktree 밖을 가리키는 경로와 큰 파일은 읽지 않는다.

ruff는 이미지에 없다. 도구 캐시(sandbox/toolcache.py)에 받은 실행 파일을 읽기 전용으로 붙인다.
컨테이너 하나에 명령 하나다: 네트워크 없음, 루트 파일 시스템 읽기 전용, 권한 없음, 자원 제한, 호스트
UID. 검사할 폴더는 읽기 전용이고 고치는 `format_apply`만 쓰기를 연다. 설정은 base 커밋에서 읽은
파일을 검사할 폴더 밖에 두고 `--config`로 넘긴다. PR이 넣은 설정 파일은 읽히지 않고(`--config`는
설정 찾기를 끈다), PR 코드가 그 사본을 바꿀 수도 없다. 설정이 없으면 `--isolated`다. `--config`의
상대 경로(per-file-ignores, exclude)는 작업 폴더(/work) 기준으로 풀린다. 파일 경로는 `--` 뒤에만
둔다 (T9). 출력은 믿지 않는 입력이다. 크기 상한 안의 JSON·diff만 읽고, 넘긴 파일 밖을 가리키면
결과를 버린다.
"""

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import tomllib
import uuid
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.models.coverage import (
    MAX_COVERAGE_XML_BYTES,
    CoverageData,
    mark_covered,
    parse_coverage_xml,
)
from app.models.install import InstallFailureKind
from app.models.test_run import KILLED_EXIT, PYTEST_TESTS_FAILED, PytestRun, ReportSummary
from app.models.text_edit import TextEdit
from app.models.tools import RUFF_CODE, RuffConfig
from app.models.violation import Violation
from app.sandbox import docker_cli, toolcache
from app.sandbox.checker_runner import (
    MAX_STDERR_CHARS,
    CheckerRun,
    Failed,
    Match,
    Ran,
    RawRun,
    check_paths,
    run_bounded,
)
from app.sandbox.container import (
    DEFAULT_TIMEOUT,
    OUT_DIR,
    PIPELINE_DIR,
    POLL_SECONDS,
    ExecResult,
    run_in_sandbox,
)
from app.sandbox.docker_cli import Image
from app.sandbox.safe_read import read_regular_file

# ---- 런타임 ----

# 런타임을 붙이는 컨테이너 안 경로. .venv의 인터프리터(`pyvenv.cfg`의 home, `bin/python`)가
# 이 경로에 묶이므로 설치 컨테이너와 테스트 컨테이너가 같아야 한다
RUNTIME_IN_CONTAINER: Final = "/opt/python"
PYTHON_IN_CONTAINER: Final = f"{RUNTIME_IN_CONTAINER}/{toolcache.TOOLS[toolcache.PYTHON].binary}"


@dataclass(frozen=True)
class Runtime:
    """도구 캐시에 받은 Python 런타임. version이 .venv 캐시 키에 들어간다."""

    version: str
    dir: Path


def ensure_runtime(cache_dir: Path, image: str) -> Runtime:
    """테스트가 쓸 Python 런타임을 도구 캐시에 받는다 (이미 있으면 받지 않는다). image는 빌드된
    샌드박스 이미지 태그다(받기 컨테이너).

    받지 못하면 SandboxError(환경 오류)다. 이 플랫폼의 배포본이 없거나 해시가 다른 것도 PR 탓이
    아니라 파이프라인 쪽 문제라서 테스트 결과를 만들지 않고 재시도와 운영 알림으로 보낸다.
    """
    version = toolcache.TOOLS[toolcache.PYTHON].default
    try:
        path = toolcache.ensure(
            toolcache.PYTHON, version, cache_dir=cache_dir, download=toolcache.fetcher(image)
        )
    except toolcache.ToolUnavailable as e:
        raise docker_cli.SandboxError(f"Python 런타임을 준비하지 못함: {e}") from e
    return Runtime(version, path)


# ---- 의존성 설치와 캐시 (.venv) ----

VENV_CACHE: Final = Path.home() / ".cache" / "pipeline" / "venvs"
# 리포 의존성과 함께 설치하는 검사 도구. pytest 자체는 리포가 고정한 버전을 따른다.
TOOL_REQUIREMENTS: Final = ("pytest-cov==6.0.0", "pytest-json-report==1.5.0")
INSTALL_TIMEOUT: Final = 900
# 컨테이너 안 .venv 경로. 스크립트의 인터프리터 경로가 여기에 묶이므로 설치·실행이 같아야 한다.
VENV_IN_CONTAINER: Final = "/work/.venv"
DEPS_IN_CONTAINER: Final = "/tmp/deps"
# 설치 방법의 버전. 설치 명령을 바꾸면 올린다 (캐시 키에 들어간다)
INSTALL_VERSION: Final = "runtime-cache-1"
# 표준 입력의 {파일 이름: 내용}을 DEPS_IN_CONTAINER에 쓰고, wheel만 설치한다. pip의 자세한
# 기록(-vv)은 실패했을 때만 줄여서 내보낸다: 건너뛴 링크 줄을 뺀 끝부분과, 소스 배포본만 있어
# 건너뛴 패키지 이름
_INSTALL_SCRIPT: Final = """\
{python} -I -c 'import json, os, sys
files = json.load(sys.stdin)
os.makedirs(sys.argv[1])
for name, text in files.items():
    with open(os.path.join(sys.argv[1], os.path.basename(name)), "w", encoding="utf-8") as f:
        f.write(text)' {deps} || exit 3
{python} -m venv {venv} || exit 4
{venv}/bin/pip install -vv --no-cache-dir --only-binary :all: {args} > /tmp/pip.log 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then
  grep -v '^ *Skipping link' /tmp/pip.log | tail -c 4000
  grep -o 'No sources permitted for [^:]*' /tmp/pip.log | sort -u
fi
exit "$rc"
"""

# pip 출력으로 설치 실패의 원인을 나눈다. 네트워크 실패에서도 "Could not find a version"이
# 함께 나오므로 네트워크 표지를 먼저 본다.
NETWORK_MARKERS: Final = (
    "NewConnectionError",
    "Failed to establish a new connection",
    "Temporary failure in name resolution",
    "Name or service not known",
    "ConnectTimeoutError",
    "ReadTimeoutError",
    "Connection reset by peer",
    "ProxyError",
    "SSLError",
)
RESOLUTION_MARKERS: Final = (
    "No matching distribution found",
    "ResolutionImpossible",
    "Could not find a version that satisfies the requirement",
    "conflicting dependencies",
    "Invalid requirement",
)
MAX_DETAIL_CHARS: Final = 2000
_NO_MATCH: Final = re.compile(r"No matching distribution found for ([A-Za-z0-9][A-Za-z0-9._-]*)")
_SOURCES_ONLY: Final = re.compile(r"No sources permitted for ([A-Za-z0-9][A-Za-z0-9._-]*)")
# 의존성 파일에서 받는 옵션 줄: 같이 넘긴 다른 의존성 파일을 읽는 것뿐이다. `--no-binary`,
# 색인 주소, `-e` 같은 옵션은 wheel만 받는다는 제한을 풀거나 다른 곳에서 받게 하므로 설치하지 않는다
_INCLUDE: Final = re.compile(r"^(?:-r|-c|--requirement|--constraint)[\s=]+([^\s/\\]+)$")
# 이름과 버전 조건이 아니라 주소·경로로 고른 의존성 (소스를 받아 빌드한다)
_DIRECT: Final = re.compile(r"://|\s@\s|^[./~]|^file:", re.IGNORECASE)


class DependencyInstallError(Exception):
    """pip이 의존성을 설치하지 못했다. 처리는 models.install.install_failure_outcome()이 정한다."""

    def __init__(
        self, kind: InstallFailureKind, cache_key: str, detail: str, packages: tuple[str, ...] = ()
    ) -> None:
        super().__init__(f"의존성 설치 실패 ({kind}): {detail[-500:]}")
        self.kind: InstallFailureKind = kind
        self.cache_key = cache_key
        self.detail = detail
        self.packages = packages  # source_only: 소스 배포본만 있는 패키지


def classify_pip_failure(output: str) -> InstallFailureKind:
    """pip 출력으로 실패 원인을 나눈다."""
    if any(marker in output for marker in NETWORK_MARKERS):
        return "network"
    if source_only_packages(output):
        return "source_only"
    if any(marker in output for marker in RESOLUTION_MARKERS):
        return "resolution"
    return "unknown"


def disallowed_lines(files: dict[str, str]) -> list[str]:
    """설치하지 않는 의존성 줄 (8.1 바이너리만 설치). 비면 설치해도 된다."""
    bad = []
    for name, text in files.items():
        for raw in text.splitlines():
            line = raw.split(" #", 1)[0].strip()
            if not line or line.startswith("#"):
                continue
            include = _INCLUDE.match(line)
            if include is not None:
                if include.group(1) not in files:
                    bad.append(f"{name}: {line}")
            elif line.startswith("-") or _DIRECT.search(line.split(";", 1)[0]):
                bad.append(f"{name}: {line}")
    return bad


def source_only_packages(output: str) -> tuple[str, ...]:
    """찾지 못한 패키지 중 소스 배포본은 있어 건너뛴 것 (`--only-binary :all:`의 -vv 기록).

    pip은 wheel이 없는 패키지와 없는 패키지를 같은 오류("No matching distribution")로 알린다.
    건너뛴 링크 기록에 그 이름이 있으면 소스 배포본만 있는 패키지다.
    """
    missing = {_normalize(m) for m in _NO_MATCH.findall(output)}
    skipped = {_normalize(m) for m in _SOURCES_ONLY.findall(output)}
    return tuple(sorted(missing & skipped))


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def pyproject_requirements(text: str) -> list[str]:
    """pyproject.toml의 의존성과 모든 optional-dependencies. 프로젝트 자신은 뺀다."""
    project = tomllib.loads(text).get("project", {})
    reqs = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        reqs.extend(group)
    own = _normalize(project.get("name", ""))
    return [
        r for r in reqs if _normalize(re.split(r"[\s\[<>=!~;@]", r.strip(), maxsplit=1)[0]) != own
    ]


def dependency_files(worktree: Path) -> list[Path]:
    """의존성 파일: 루트의 requirements*.txt와 pyproject.toml. lock 파일은 아직 쓰지 않는다."""
    files = sorted(worktree.glob("requirements*.txt"))
    if (worktree / "pyproject.toml").is_file():
        files.append(worktree / "pyproject.toml")
    return files


def cache_key(worktree: Path, image: Image, runtime_version: str) -> str:
    """의존성 파일 내용 + 이미지 id + 런타임 버전 + 검사 도구 버전 + 설치 방법 버전."""
    h = hashlib.sha256()
    for part in (image.id, runtime_version, *TOOL_REQUIREMENTS, INSTALL_VERSION):
        h.update(part.encode() + b"\0")
    for path in dependency_files(worktree):
        h.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return h.hexdigest()[:24]


def deps_input(worktree: Path) -> tuple[dict[str, str], list[str]]:
    """설치 컨테이너에 표준 입력으로 넘길 의존성 파일 {이름: 내용}과 pip 인자."""
    files: dict[str, str] = {}
    args: list[str] = []
    for path in dependency_files(worktree):
        if path.name == "pyproject.toml":
            reqs = pyproject_requirements(path.read_text())
            name, text = "pyproject-deps.txt", "\n".join(reqs) + "\n"
        else:
            name, text = path.name, path.read_text(encoding="utf-8", errors="replace")
        files[name] = text
        args += ["-r", f"{DEPS_IN_CONTAINER}/{name}"]
    return files, [*args, *TOOL_REQUIREMENTS]


def install_script(pip_args: list[str]) -> str:
    return _INSTALL_SCRIPT.format(
        python=PYTHON_IN_CONTAINER,
        deps=DEPS_IN_CONTAINER,
        venv=VENV_IN_CONTAINER,
        args=shlex.join(pip_args),
    )


def ensure_venv(
    worktree: Path,
    image: Image,
    runtime: Runtime,
    cache_dir: Path = VENV_CACHE,
    tag: str = docker_cli.IMAGE,
    *,
    memory_gb: float = docker_cli.MEMORY_LIMIT_GB,
) -> Path:
    """캐시 키에 맞는 .venv 경로를 돌려준다. 없으면 설치 컨테이너에서 만든다.

    임시 폴더에 설치하고 완료 표시를 남긴 뒤 캐시 경로로 옮겨, 실패한 설치가 캐시에 남지 않게 한다.
    docker 자체가 실패하면 SandboxError, pip이 실패하면 DependencyInstallError를 올린다.
    """
    key = cache_key(worktree, image, runtime.version)
    target = cache_dir / key
    if (target / ".complete").is_file():
        return target

    cache_dir.mkdir(parents=True, exist_ok=True)
    build = cache_dir / f".{key}.{uuid.uuid4().hex[:8]}.tmp"
    build.mkdir()
    try:
        files, pip_args = deps_input(worktree)
        bad = disallowed_lines(files)
        if bad:
            detail = "wheel만 설치하므로 받지 않는 줄:\n" + "\n".join(bad[:20])
            raise DependencyInstallError("disallowed", key, detail[:MAX_DETAIL_CHARS])
        proc = _run_install(
            build, runtime, tag, install_script(pip_args), json.dumps(files), memory_gb
        )
        if proc.returncode in docker_cli.DOCKER_FAILED:
            raise docker_cli.SandboxError(
                f"설치 컨테이너 실행 실패 ({proc.returncode}): {proc.stderr.strip()[-500:]}"
            )
        if proc.returncode != 0:
            output = proc.stdout + proc.stderr
            raise DependencyInstallError(
                classify_pip_failure(output), key, output[-MAX_DETAIL_CHARS:],
                packages=source_only_packages(output),
            )  # fmt: skip
        marker = build / ".complete"
        marker.unlink(missing_ok=True)  # 설치 컨테이너가 남긴 것(링크일 수 있음)을 따라가지 않는다
        marker.touch(exist_ok=False)
        try:
            build.rename(target)
        except OSError:  # 다른 작업이 같은 키를 먼저 만들었다
            if not (target / ".complete").is_file():
                raise
    finally:
        if build.exists():
            shutil.rmtree(build, ignore_errors=True)
    return target


def _run_install(
    build: Path, runtime: Runtime, tag: str, script: str, stdin: str, memory_gb: float
) -> subprocess.CompletedProcess[str]:
    """설치 컨테이너를 만들고 런타임을 복사해 넣은 뒤 설치 스크립트를 돌린다.

    런타임은 마운트하지 않고 복사한다. 네트워크가 열린 이 컨테이너에 붙는 호스트 경로는 빈 출력
    폴더(build) 하나뿐이다 (8.1: 다른 캐시 항목을 마운트하지 않는다).
    """
    name = f"pr-proof-install-{uuid.uuid4().hex[:12]}"
    docker_cli.run(
        "create", "-i", "--name", name, *docker_cli.limits(memory_gb),
        "--user", docker_cli.user(), "-e", "HOME=/tmp", "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-e", "PIP_DISABLE_PIP_VERSION_CHECK=1", "-e", "PIP_NO_INPUT=1",
        "-v", f"{build}:{VENV_IN_CONTAINER}",
        tag, "sh", "-c", script,
    )  # fmt: skip
    try:
        docker_cli.run("cp", str(runtime.dir.resolve()), f"{name}:{RUNTIME_IN_CONTAINER}")
        return docker_cli.run(
            "start", "-ai", name, timeout=INSTALL_TIMEOUT, check=False, input=stdin
        )
    finally:
        docker_cli.remove_container(name)


# ---- 테스트 실행과 결과 읽기 (pytest, report.json) ----

REPORT_FILE: Final = f"{OUT_DIR}/report.json"
RERUN_REPORT_FILE: Final = f"{OUT_DIR}/rerun.json"
# 이보다 많이 실패하면 불안정이 아니라 실제 실패로 보고 다시 돌리지 않는다
MAX_RERUN_TESTS: Final = 20
MAX_REPORT_BYTES: Final = 50 * 1024 * 1024  # 큰 테스트 묶음의 report.json도 들어가게
COVERAGE_XML: Final = f"{OUT_DIR}/coverage.xml"
# coverage.xml 경로를 리포 기준 상대 경로로 남기는 파이프라인 쪽 설정 (docker/sandbox/coveragerc)
COVERAGERC: Final = f"{PIPELINE_DIR}/coveragerc"
MAX_OUTPUT_CHARS: Final = 4000


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


def rerun_argv(node_ids: list[str]) -> list[str]:
    """실패한 테스트만 한 번 더. coverage는 처음 실행 것을 쓰므로 다시 만들지 않는다."""
    return [
        ".venv/bin/pytest", "-p", "no:cacheprovider",
        "--json-report", f"--json-report-file={RERUN_REPORT_FILE}",
        "--", *node_ids,
    ]  # fmt: skip


def _read_json(report: Path) -> object:
    """PR 코드가 바꿀 수 있는 결과 파일. 일반 파일만 읽는다 (링크·FIFO면 없는 것으로 본다)."""
    data = read_regular_file(report, max_bytes=MAX_REPORT_BYTES)
    if data is None:
        return None
    return json.loads(data)


def passed_ids(report: Path) -> set[str]:
    try:
        data = _read_json(report)
        return {t["nodeid"] for t in data.get("tests", []) if t.get("outcome") == "passed"}
    except (ValueError, KeyError, TypeError, AttributeError):
        return set()


def read_summary(report: Path) -> ReportSummary | None:
    """report.json을 읽는다. 없거나(링크·FIFO 포함) 깨졌으면 None."""
    try:
        data = _read_json(report)
        return None if data is None else ReportSummary.from_report(data)
    except (ValueError, KeyError, TypeError, AttributeError):
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
    oom_killed = _check_environment(container, result, has_report=summary is not None)
    flaky = _rerun_failed(container, worktree, result, summary, timeout, is_stale, poll_seconds)
    return PytestRun(
        flaky=flaky,
        exit_code=result.returncode,
        summary=summary,
        report_path=report if summary is not None else None,
        coverage_xml=coverage_xml if coverage_xml.is_file() else None,
        timed_out=result.timed_out,
        cancelled=result.cancelled,
        oom_killed=oom_killed,
        output=(result.stdout + result.stderr)[-MAX_OUTPUT_CHARS:],
    )


def _rerun_failed(
    container: str,
    worktree: Path,
    result: ExecResult,
    summary: ReportSummary | None,
    timeout: int,
    is_stale: Callable[[], bool] | None,
    poll_seconds: float,
) -> tuple[str, ...]:
    """실패한 테스트를 한 번 다시 돌려, 통과한 것(flaky)을 돌려준다 (DESIGN.md F8).

    테스트 실패(종료 코드 1)일 때만 한다. 수집 오류·테스트 오류는 다시 돌려도 같으므로 하지 않는다.
    """
    if result.returncode != PYTEST_TESTS_FAILED or summary is None or summary.errors:
        return ()
    failed = list(summary.failed_tests)
    if not failed or len(failed) > MAX_RERUN_TESTS:
        return ()
    rerun_report = worktree / RERUN_REPORT_FILE
    rerun_report.unlink(missing_ok=True)
    rerun = run_in_sandbox(
        container,
        rerun_argv(failed),
        env={"PYTHONPATH": pythonpath_for(worktree), "PYTHONDONTWRITEBYTECODE": "1"},
        timeout=timeout,
        is_stale=is_stale,
        poll_seconds=poll_seconds,
    )
    if rerun.cancelled or rerun.timed_out or rerun.returncode in docker_cli.DOCKER_FAILED:
        return ()  # 처음 결과(실패)를 그대로 쓴다
    passed = passed_ids(rerun_report)
    return tuple(t for t in failed if t in passed)


def _check_environment(container: str, result: ExecResult, *, has_report: bool) -> bool:
    """PR 코드 탓이 아닌 실패를 SandboxError(환경 오류)로 올리고, OOM 종료 여부를 돌려준다.

    취소·시간 초과를 먼저 거른다 (그때는 파이프라인이 컨테이너를 지웠다).
    docker exec 자체가 실패했거나, 결과 파일이 없는데 컨테이너가 OOM이 아닌 이유로 사라졌으면
    환경 오류다. 메모리 한도로 컨테이너가 죽은 것은 PR 코드 문제(unrunnable)다.
    """
    returncode = result.returncode
    if result.cancelled or result.timed_out or returncode is None:
        return False
    if returncode in docker_cli.DOCKER_FAILED:
        raise docker_cli.SandboxError(
            f"docker exec 실패 ({returncode}): {result.stderr.strip()[-500:]}"
        )
    if has_report or returncode == 0:
        return False
    state = docker_cli.container_state(container)
    if state is not None and state.oom_killed:
        return True
    if state is None or not state.running:
        raise docker_cli.SandboxError(f"컨테이너 {container}가 실행 중이 아니다 ({returncode})")
    return False


# ---- 커버리지 읽기와 줄 대응 (coverage.xml) ----

MAX_SOURCE_BYTES: Final = 2 * 1024 * 1024


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


def _path_inside(worktree: Path, rel: str) -> Path | None:
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
        path = _path_inside(worktree, rel)
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
    findings: Iterable[Violation], coverage_xml: Path | None, worktree: Path
) -> tuple[Violation, ...]:
    """위반에 covered를 채운다. coverage.xml은 `PytestRun.coverage_xml`(수정 전 실행)이다."""
    findings = tuple(findings)
    coverage = read_coverage(coverage_xml)
    if coverage is None:
        return mark_covered(findings, None)
    return mark_covered(findings, coverage, read_sources(worktree, (f.file for f in findings)))


# ---- 도구 ruff, ruff-format ----

RUFF_WORK: Final = "/work"
RUFF_MOUNT: Final = "/tools/ruff"
CONFIG_MOUNT: Final = "/config"
RUFF_TIMEOUT: Final = 120
SYNTAX_ERROR_CODE: Final = "syntax-error"
SAFE_FIX: Final = "safe"  # 동작을 바꾸지 않는 수정. unsafe·display-only는 적용하지 않는다
FORMAT_MESSAGE: Final = "ruff format이 이 줄을 바꾼다"
# ruff-format이 찾은 위반의 코드. ruff의 규칙 코드가 아니다
FORMAT_CODE: Final = "format"
# ruff 종료 코드: 0 위반 없음(포맷 그대로), 1 위반 있음(포맷이 바뀜), 2 설정·인자 오류 등
_RUFF_OK: Final = frozenset({0, 1})

# (포맷돼 있음 또는 적용함) True, (포맷돼 있지 않음) False, 오류는 그 이유
FormatResult = bool | str


@contextmanager
def _ruff_container(
    root: Path,
    *,
    tool_dir: Path,
    image: str,
    config: RuffConfig | None,
    writable: bool = False,
    memory_gb: float = docker_cli.MEMORY_LIMIT_GB,
) -> Iterator[tuple[list[str], list[str]]]:
    """(docker 인자, ruff 설정 인자). 설정 사본은 검사할 폴더 밖의 임시 폴더에 둔다."""
    with tempfile.TemporaryDirectory(prefix="pipeline-ruff-") as tmp:
        mounts = ["-v", f"{root.resolve()}:{RUFF_WORK}{'' if writable else ':ro'}",
                  "-v", f"{tool_dir.resolve()}:{RUFF_MOUNT}:ro"]  # fmt: skip
        if config is not None:
            (Path(tmp) / config.name).write_text(config.text)
            mounts += ["-v", f"{tmp}:{CONFIG_MOUNT}:ro"]
            config_args = ["--config", f"{CONFIG_MOUNT}/{config.name}"]
        else:
            config_args = ["--isolated"]
        docker = [
            "run", "--rm", "-i", "--network", "none", "--read-only", *docker_cli.limits(memory_gb),
            "--user", docker_cli.user(), "-e", "HOME=/tmp", *mounts, "-w", RUFF_WORK,
            image, f"{RUFF_MOUNT}/ruff",
        ]  # fmt: skip
        yield docker, config_args


def _ruff_failure(result: RawRun, timeout: int, what: str) -> CheckerRun | None:
    """결과를 쓸 수 없으면 그 이유. docker 자체가 실패했으면 SandboxError (환경 오류)."""
    stderr = result.stderr.strip()[-MAX_STDERR_CHARS:]
    if result.too_large:
        return Failed(f"{what} 출력이 크기 상한을 넘음")
    if result.timed_out:
        return Failed(f"{what}이 {timeout}초 안에 끝나지 않음")
    if result.returncode in docker_cli.DOCKER_FAILED:
        raise docker_cli.SandboxError(f"docker run 실패 ({result.returncode}): {stderr}")
    if result.returncode == KILLED_EXIT:
        return Failed(f"{what}이 메모리 한도를 넘었거나 강제로 끝남")
    if result.returncode not in _RUFF_OK:
        return Failed(f"{what} 실행 오류: {stderr or f'종료 코드 {result.returncode}'}")
    return None


def ruff_check(
    root: Path,
    files: Sequence[str],
    *,
    tool_dir: Path,
    image: str,
    config: RuffConfig | None,
    codes: Sequence[str] = (),
    timeout: int = RUFF_TIMEOUT,
) -> CheckerRun:
    """root 아래 files에 `ruff check`를 돌린다. codes가 있으면 그 규칙만(`--select`), 없으면 설정의
    규칙이다. 명시한 파일도 설정의 exclude를 따르게 `--force-exclude`를 쓴다."""
    check_paths(files)
    if not files:
        return Ran(())
    select = ["--select", ",".join(codes)] if codes else []
    with _ruff_container(root, tool_dir=tool_dir, image=image, config=config) as (
        docker,
        config_args,
    ):
        argv = [*docker, "check", "--no-cache", "--output-format", "json", "--force-exclude",
                *config_args, *select, "--", *files]  # fmt: skip
        result = run_bounded(argv, b"", timeout=timeout)
    failed = _ruff_failure(result, timeout, "ruff")
    if failed is not None:
        return failed
    try:
        matches = parse_ruff_check(result.stdout.decode(errors="replace"))
    except (ValueError, KeyError, TypeError) as e:
        return Failed(f"ruff 출력을 읽지 못함 ({e})")
    return _only_given_files(matches, files)


def _only_given_files(matches: tuple[Match, ...], files: Sequence[str]) -> CheckerRun:
    outside = sorted({m.file for m in matches} - set(files))
    if outside:
        return Failed(f"출력이 넘기지 않은 파일을 가리킴: {', '.join(outside[:5])}")
    return Ran(matches)


def _repo_path(filename: str) -> str:
    prefix = RUFF_WORK + "/"
    return filename[len(prefix) :] if filename.startswith(prefix) else filename


def parse_ruff_check(stdout: str) -> tuple[Match, ...]:
    """ruff JSON 출력을 읽는다. 파일 경로는 /work/ 기준이라 저장소 기준으로 바꾼다."""
    items = json.loads(stdout or "[]")
    if not isinstance(items, list):
        raise TypeError("최상위가 목록이 아님")
    out = []
    for item in items:
        start = item["location"]["row"]
        end = (item.get("end_location") or {}).get("row", start)
        if not (isinstance(start, int) and isinstance(end, int)) or start < 1 or end < start:
            raise ValueError(f"줄 범위가 틀림: {start!r}–{end!r}")
        out.append(
            Match(
                file=_repo_path(item["filename"]),
                line=start,
                end_line=end,
                message=str(item["message"]),
                code=item.get("code") or SYNTAX_ERROR_CODE,
                edits=_safe_edits(item.get("fix")),
            )
        )
    return tuple(out)


def _safe_edits(fix: dict | None) -> tuple[TextEdit, ...]:
    """안전한 수정의 편집 목록. 위치는 1부터 세는 줄·열(문자 단위)이다."""
    if not fix or fix.get("applicability") != SAFE_FIX:
        return ()
    return tuple(
        TextEdit(
            row=e["location"]["row"],
            column=e["location"]["column"],
            end_row=e["end_location"]["row"],
            end_column=e["end_location"]["column"],
            content=e.get("content") or "",
        )
        for e in fix.get("edits") or ()
    )


def format_diff(
    root: Path,
    files: Sequence[str],
    *,
    tool_dir: Path,
    image: str,
    config: RuffConfig | None,
    timeout: int = RUFF_TIMEOUT,
) -> CheckerRun:
    """`ruff format --diff`가 바꿀 줄. 바꿀 덩어리마다 Match 하나다 (원래 파일의 줄 번호)."""
    check_paths(files)
    if not files:
        return Ran(())
    with _ruff_container(root, tool_dir=tool_dir, image=image, config=config) as (
        docker,
        config_args,
    ):
        argv = [*docker, "format", "--no-cache", "--force-exclude", "--diff", *config_args, "--",
                *files]  # fmt: skip
        result = run_bounded(argv, b"", timeout=timeout)
    failed = _ruff_failure(result, timeout, "ruff format")
    if failed is not None:
        return failed
    return _only_given_files(parse_format_diff(result.stdout.decode(errors="replace")), files)


def parse_format_diff(diff: str) -> tuple[Match, ...]:
    """unified diff에서 원래 파일의 바뀌는 줄 범위. 줄을 더하기만 하는 덩어리는 그 앞줄이다."""
    out: list[Match] = []
    file = ""
    old = 0
    changed: list[int] = []

    def close() -> None:
        if file and changed:
            out.append(Match(file, min(changed), max(changed), FORMAT_MESSAGE, FORMAT_CODE))
        changed.clear()

    for line in diff.splitlines():
        if line.startswith("--- "):
            close()
            file = _repo_path(line[4:].split("\t")[0].strip())
        elif line.startswith("+++ "):
            continue
        elif line.startswith("@@"):
            close()
            old = int(line.split()[1].lstrip("-").split(",")[0])
        elif line.startswith("-"):
            changed.append(old)
            old += 1
        elif line.startswith("+"):
            changed.append(max(1, old - 1))
        elif line.startswith(" "):
            close()
            old += 1
    close()
    return tuple(out)


def format_check(
    root: Path,
    *,
    tool_dir: Path,
    image: str,
    config: RuffConfig | None,
    timeout: int = RUFF_TIMEOUT,
) -> FormatResult:
    """root 전체가 `ruff format --check`를 통과하는지 (4.7: base가 포맷돼 있을 때만 포맷한다)."""
    with _ruff_container(root, tool_dir=tool_dir, image=image, config=config) as (
        docker,
        config_args,
    ):
        argv = [*docker, "format", "--no-cache", "--force-exclude", "--check", *config_args, "--",
                "."]  # fmt: skip
        result = run_bounded(argv, b"", timeout=timeout)
    failed = _ruff_failure(result, timeout, "ruff format")
    if isinstance(failed, Failed):
        return failed.reason
    return result.returncode == 0


def format_apply(
    root: Path,
    files: Sequence[str],
    *,
    tool_dir: Path,
    image: str,
    config: RuffConfig | None,
    timeout: int = RUFF_TIMEOUT,
) -> FormatResult:
    """files를 `ruff format`으로 고친다. root를 쓰기로 붙이는 유일한 실행이다."""
    check_paths(files)
    if not files:
        return True
    with _ruff_container(root, tool_dir=tool_dir, image=image, config=config, writable=True) as (
        docker,
        args,
    ):
        argv = [*docker, "format", "--no-cache", "--force-exclude", *args, "--", *files]
        result = run_bounded(argv, b"", timeout=timeout)
    failed = _ruff_failure(result, timeout, "ruff format")
    if isinstance(failed, Failed):
        return failed.reason
    return result.returncode == 0 or f"ruff format 종료 코드 {result.returncode}"


def ruff_rule_exists(code: str, *, tool_dir: Path, image: str, timeout: int = 30) -> bool:
    """`ruff rule <코드>`가 그 규칙을 아는지 (4.4: 모델이 고른 코드가 실재하는지)."""
    if not RUFF_CODE.match(code):  # 형식이 맞는 코드만 인자로 넘긴다 (`-`로 시작하는 옵션이 아님)
        return False
    with tempfile.TemporaryDirectory(prefix="pipeline-ruff-") as tmp:
        with _ruff_container(Path(tmp), tool_dir=tool_dir, image=image, config=None) as (docker, _):
            result = run_bounded([*docker, "rule", code], b"", timeout=timeout)
    if result.returncode in docker_cli.DOCKER_FAILED:
        raise docker_cli.SandboxError(f"docker run 실패 ({result.returncode}): {result.stderr}")
    return result.returncode == 0 and not result.timed_out
