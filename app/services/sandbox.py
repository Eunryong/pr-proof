"""테스트 샌드박스 서비스: Docker 이미지, .venv 준비와 캐시, run_in_sandbox(), run_tests().

설계는 docs/DESIGN.md의 F5와 "보안·인증 > 샌드박스"를 따른다.
PR 코드는 이 모듈이 띄운 컨테이너 안에서만 실행한다. 호스트에서는 docker CLI만 부른다.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import tomllib
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from app.models.sandbox import ExecResult, Image
from app.models.test_run import PytestRun, ReportSummary

IMAGE = "pipeline-sandbox"
SANDBOX_DIR = Path(__file__).resolve().parents[2] / "sandbox"
COVERAGERC = SANDBOX_DIR / "coveragerc"
VENV_CACHE = Path.home() / ".cache" / "pipeline" / "venvs"

# 대상 리포의 의존성과 함께 .venv에 설치하는 검사 도구. pytest 자체는 리포가 고정한 버전을 따른다.
TOOL_REQUIREMENTS = ("pytest-cov==6.0.0", "pytest-json-report==1.5.0")

OUT_DIR = ".pipeline-out"
POLL_SECONDS = 10.0
TEST_TIMEOUT = 600
INSTALL_TIMEOUT = 900
# docker exec가 `timeout`보다 이만큼 더 걸리면 호스트에서 컨테이너를 지운다.
HOST_GRACE_SECONDS = 30
TIMEOUT_EXIT = 124  # coreutils `timeout`의 종료 코드

LIMITS = ("--memory", "1g", "--cpus", "1", "--pids-limit", "256")

_ENV_PASSTHROUGH = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG")


class SandboxError(RuntimeError):
    """Docker나 .venv 설치가 실패한 환경 오류. PR의 테스트 실패와 구분한다."""


def minimal_env() -> dict[str, str]:
    """docker CLI가 동작하는 데 필요한 변수만 넘긴다."""
    return {k: os.environ[k] for k in _ENV_PASSTHROUGH if k in os.environ}


def _docker(*args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            env=minimal_env(),
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SandboxError(f"docker {args[0]} 실행 실패: {e}") from e
    if proc.returncode != 0:
        raise SandboxError(
            f"docker {args[0]} 실패 ({proc.returncode}): {proc.stderr.strip()[-2000:]}"
        )
    return proc


def _user() -> str:
    return f"{os.getuid()}:{os.getgid()}"


def ensure_image(tag: str = IMAGE) -> Image:
    """베이스 이미지를 빌드(캐시되면 즉시 끝남)하고 id와 Python 버전을 돌려준다."""
    _docker("build", "-q", "-t", tag, str(SANDBOX_DIR), timeout=600)
    info = json.loads(_docker("image", "inspect", tag, "--format", "{{json .}}").stdout)
    env = dict(item.split("=", 1) for item in info["Config"]["Env"] if "=" in item)
    return Image(id=info["Id"], python_version=env.get("PYTHON_VERSION", "unknown"))


# --- .venv 준비 ---------------------------------------------------------------


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


def cache_key(worktree: Path, image: Image) -> str:
    """의존성 파일 내용 + 베이스 이미지 id + Python 버전 + 검사 도구 버전."""
    h = hashlib.sha256()
    for part in (image.id, image.python_version, *TOOL_REQUIREMENTS):
        h.update(part.encode() + b"\0")
    for path in dependency_files(worktree):
        h.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return h.hexdigest()[:24]


def _write_deps(worktree: Path, dest: Path) -> list[str]:
    """설치 컨테이너에 넘길 의존성 파일을 dest에 만들고 pip 인자를 돌려준다."""
    args: list[str] = []
    for path in dependency_files(worktree):
        if path.name == "pyproject.toml":
            reqs = pyproject_requirements(path.read_text())
            (dest / "pyproject-deps.txt").write_text("\n".join(reqs) + "\n")
            args += ["-r", "/deps/pyproject-deps.txt"]
        else:
            shutil.copy2(path, dest / path.name)
            args += ["-r", f"/deps/{path.name}"]
    return [*args, *TOOL_REQUIREMENTS]


def ensure_venv(
    worktree: Path, image: Image, cache_dir: Path = VENV_CACHE, tag: str = IMAGE
) -> Path:
    """캐시 키에 맞는 .venv를 돌려준다. 없으면 네트워크가 열린 설치 컨테이너에서 만든다.

    .venv에는 의존성만 넣고 대상 리포 자체는 설치하지 않는다. 설치 컨테이너에는 의존성 파일만
    마운트하고, .venv는 테스트 컨테이너와 같은 경로(/work/.venv)에 만들어 스크립트 경로를 맞춘다.
    """
    key = cache_key(worktree, image)
    target = cache_dir / key
    if (target / ".complete").is_file():
        return target

    cache_dir.mkdir(parents=True, exist_ok=True)
    build = cache_dir / f".{key}.{uuid.uuid4().hex[:8]}.tmp"
    build.mkdir()
    try:
        with tempfile.TemporaryDirectory(prefix="pipeline-deps-") as deps:
            pip_args = _write_deps(worktree, Path(deps))
            script = (
                "python -m venv /work/.venv && /work/.venv/bin/pip install --no-cache-dir "
                + shlex.join(pip_args)
            )
            _docker(
                "run", "--rm", *LIMITS, "--user", _user(), "-e", "HOME=/tmp",
                "-v", f"{deps}:/deps:ro",
                "-v", f"{build}:/work/.venv",
                tag, "sh", "-c", script,
                timeout=INSTALL_TIMEOUT,
            )  # fmt: skip
        (build / ".complete").touch()
        try:
            build.rename(target)
        except OSError:  # 다른 작업이 같은 키를 먼저 만들었다
            if not (target / ".complete").is_file():
                raise
    finally:
        if build.exists():
            shutil.rmtree(build, ignore_errors=True)
    return target


# --- 컨테이너와 실행 ----------------------------------------------------------


def remove_container(name: str) -> None:
    subprocess.run(
        ["docker", "rm", "-f", name],
        capture_output=True,
        env=minimal_env(),
        timeout=60,
        check=False,
    )


@contextmanager
def sandbox(
    worktree: Path, venv: Path, *, job_id: str | None = None, tag: str = IMAGE
) -> Iterator[str]:
    """작업용 컨테이너를 띄우고 이름을 돌려준다. 블록을 나가면 지운다.

    네트워크 없음, 자원 제한, 호스트 UID·GID. .venv와 coverage 설정은 읽기 전용.
    """
    name = f"job-{job_id or uuid.uuid4().hex[:12]}"
    (worktree / OUT_DIR).mkdir(exist_ok=True)
    _docker(
        "run", "-d", "--name", name, "--network", "none", *LIMITS,
        "--user", _user(), "-e", "HOME=/tmp",
        "-v", f"{worktree.resolve()}:/work", "-w", "/work",
        "-v", f"{venv.resolve()}:/work/.venv:ro",
        "-v", f"{COVERAGERC}:/pipeline/coveragerc:ro",
        tag, "sleep", "infinity",
    )  # fmt: skip
    try:
        yield name
    finally:
        remove_container(name)


def run_in_sandbox(
    container: str,
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: int = TEST_TIMEOUT,
    is_stale: Callable[[], bool] | None = None,
    poll_seconds: float = POLL_SECONDS,
) -> ExecResult:
    """컨테이너 안에서 명령을 실행한다. pytest, mutmut, ruff가 모두 이 함수를 거친다.

    끝날 때까지 poll_seconds마다 is_stale()을 확인해, 커밋이 낡았으면 컨테이너를 지우고
    cancelled로 돌려준다. 컨테이너 안의 `timeout`이 듣지 않으면 호스트에서 지운다.
    """
    cmd = ["docker", "exec"]
    for k, v in (env or {}).items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [container, "timeout", str(timeout), *argv]

    cancelled = timed_out = False
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdout=out, stderr=err, env=minimal_env())
        deadline = time.monotonic() + timeout + HOST_GRACE_SECONDS
        while True:
            try:
                proc.wait(timeout=poll_seconds)
                break
            except subprocess.TimeoutExpired:
                pass
            if is_stale is not None and is_stale():
                cancelled = True
            elif time.monotonic() > deadline:
                timed_out = True
            else:
                continue
            remove_container(container)  # 컨테이너가 지워지면 docker exec도 끝난다
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            break
        out.seek(0)
        err.seek(0)
        stdout = out.read().decode(errors="replace")
        stderr = err.read().decode(errors="replace")

    if proc.returncode == TIMEOUT_EXIT and not cancelled:
        timed_out = True
    return ExecResult(
        returncode=None if cancelled else proc.returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        cancelled=cancelled,
    )


# --- pytest ------------------------------------------------------------------


def pythonpath_for(worktree: Path) -> str:
    """리포를 설치하지 않으므로 PYTHONPATH로 import 경로를 준다. src 구조면 /work/src도 넣는다."""
    return "/work/src:/work" if (worktree / "src").is_dir() else "/work"


def run_tests(
    container: str,
    worktree: Path,
    *,
    timeout: int = TEST_TIMEOUT,
    is_stale: Callable[[], bool] | None = None,
    poll_seconds: float = POLL_SECONDS,
) -> PytestRun:
    """샌드박스에서 pytest를 실행하고 결과 JSON과 coverage.xml을 .pipeline-out/에 남긴다."""
    out = worktree / OUT_DIR
    report = out / "report.json"
    coverage_xml = out / "coverage.xml"
    for stale in (report, coverage_xml, out / ".coverage"):
        stale.unlink(missing_ok=True)

    result = run_in_sandbox(
        container,
        [
            ".venv/bin/pytest",
            "-p", "no:cacheprovider",
            "--json-report", f"--json-report-file={OUT_DIR}/report.json",
            "--cov", "--cov-config=/pipeline/coveragerc",
            f"--cov-report=xml:{OUT_DIR}/coverage.xml",
        ],
        env={"PYTHONPATH": pythonpath_for(worktree), "PYTHONDONTWRITEBYTECODE": "1"},
        timeout=timeout,
        is_stale=is_stale,
        poll_seconds=poll_seconds,
    )  # fmt: skip

    summary: ReportSummary | None = None
    if report.is_file():
        try:
            summary = ReportSummary.from_report(json.loads(report.read_text()))
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
            summary = None
    return PytestRun(
        exit_code=result.returncode,
        summary=summary,
        report_path=report if summary is not None else None,
        coverage_xml=coverage_xml if coverage_xml.is_file() else None,
        timed_out=result.timed_out,
        cancelled=result.cancelled,
        output=(result.stdout + result.stderr)[-4000:],
    )
