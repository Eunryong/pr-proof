"""구운 층 위에서 처방의 빌드·테스트 단계를 저장소 폴더에 돌린다 (DESIGN 7.10, 8.1).

작업 컨테이너는 하나이고 단계는 `docker exec`로 넣는다. 네트워크가 없고 호스트 UID로 돈다. 저장소
폴더는 /work에 읽기·쓰기로 붙는다. 그 폴더는 테스트 중인 코드가 무엇이든 쓸 수 있어, 거기서 읽는
결과 파일은 링크를 따라가지 않는 일반 파일만 크기 상한 안에서 읽는다.
"""

import shutil
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.worker.env import install_pure
from app.worker.env.install import Layers, Limits
from app.worker.env.recipe_pure import OUT_DIR, EnvRecipe, Step, TestStep
from app.worker.env.run_pure import judge_test
from app.worker.sandbox import container, docker_cli
from app.worker.sandbox.container import ExecResult, run_in_sandbox
from app.worker.sandbox.docker_cli import SandboxError
from app.worker.sandbox.safe_read import read_regular_file
from app.worker.testing.reports_pure import MAX_JUNIT_BYTES
from app.worker.testing.test_run_pure import StepRun, TestRun

MAX_OUTPUT_CHARS: Final = 4000


@dataclass(frozen=True)
class Environment:
    """작업 컨테이너 하나와 처방. run을 여러 번 불러도 같은 컨테이너에서 돈다."""

    recipe: EnvRecipe
    worktree: Path
    container: str
    limits: Limits

    def run(self, *, is_stale: Callable[[], bool] | None = None) -> TestRun:
        """빌드 단계를 차례로 돌리고(처음 실패에서 멈춤) 테스트 단계를 돌린다."""
        test = self.recipe.test
        report = None if test is None or not test.report_path else self.worktree / test.report_path
        _clear_out_dir(self.worktree, report)
        build: list[StepRun] = []
        for step in self.recipe.build:
            ran = self._step(step, is_stale)
            build.append(ran)
            if ran.exit_code != 0 or ran.timed_out or ran.cancelled:
                return TestRun(build=tuple(build))
        if test is None:
            return TestRun(build=tuple(build), verdict="no_tests")
        ran = self._step(test, is_stale)
        data = None if report is None else _read_report(self.worktree, report)
        verdict, why, summary = judge_test(test.report, ran.exit_code, data, test.report_path)
        return TestRun(
            build=tuple(build),
            test=ran,
            verdict=verdict,
            unrunnable_why=why,
            report=summary,
            report_path=report if summary is not None else None,
        )

    def _step(self, step: Step | TestStep, is_stale: Callable[[], bool] | None) -> StepRun:
        started = time.monotonic()
        result = run_in_sandbox(
            self.container,
            ["sh", "-c", step.command],
            env=dict(self.recipe.env),
            timeout=self.limits.step_timeout_s,
            is_stale=is_stale,
        )
        return StepRun(
            step.name,
            result.returncode,
            timed_out=result.timed_out,
            cancelled=result.cancelled,
            oom_killed=self._oom_killed(result),
            output=install_pure.output_tail(result.stdout, result.stderr, MAX_OUTPUT_CHARS),
            seconds=time.monotonic() - started,
        )

    def _oom_killed(self, result: ExecResult) -> bool:
        """PR 코드 탓이 아닌 실패를 SandboxError로 올리고, 메모리 한도로 죽었는지 돌려준다.

        취소·시간 초과에는 파이프라인이 컨테이너를 지웠으므로 보지 않는다. docker exec 자체가
        실패했거나 컨테이너가 메모리 한도가 아닌 이유로 사라졌으면 환경 오류다.
        """
        if result.cancelled or result.timed_out or result.returncode in (None, 0):
            return False
        if install_pure.is_docker_failure(result.returncode, result.stderr):
            raise SandboxError(f"docker exec 실패 ({result.returncode}): {result.stderr[-500:]}")
        state = docker_cli.container_state(self.container)
        if state is not None and state.oom_killed:
            return True
        if state is None or not state.running:
            raise SandboxError(
                f"컨테이너 {self.container}가 실행 중이 아니다 ({result.returncode})"
            )
        return False


@contextmanager
def open_environment(
    layers: Layers,
    recipe: EnvRecipe,
    worktree: Path,
    limits: Limits,
    *,
    names: docker_cli.Names = docker_cli.DEFAULT_NAMES,
    job_id: str | None = None,
) -> Iterator[Environment]:
    """마지막 층에서 worktree를 /work에 붙인 작업 컨테이너를 띄운다. 블록을 나가면 지운다.

    worktree는 컨테이너를 띄울 때 붙인다. 실행 중인 컨테이너에는 마운트를 더할 수 없다.
    """
    _clear_out_dir(worktree, None)
    with container.running(
        worktree,
        (),
        job_id=job_id,
        memory_gb=limits.memory_gb,
        cpus=limits.cpus,
        names=names,
        image=layers.top,
    ) as name:
        yield Environment(recipe, worktree, name, limits)


def _clear_out_dir(worktree: Path, report: Path | None) -> None:
    """.pipeline-out/을 폴더로 두고 이 처방의 결과 파일을 지운다. 링크나 파일이면 그것을 지운다."""
    out = worktree / OUT_DIR
    if out.is_symlink() or (out.exists() and not out.is_dir()):
        out.unlink()
    out.mkdir(exist_ok=True)
    if report is not None and report.is_dir() and not report.is_symlink():
        shutil.rmtree(report)
    elif report is not None:
        report.unlink(missing_ok=True)


def _read_report(worktree: Path, report: Path) -> bytes | None:
    """결과 파일. .pipeline-out/이 링크로 바뀌었거나 일반 파일이 아니면 None."""
    out = worktree / OUT_DIR
    if out.is_symlink() or not out.is_dir():
        return None
    return read_regular_file(report, max_bytes=MAX_JUNIT_BYTES)
