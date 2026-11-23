"""평가 세트를 한 명령으로 돌리고 `eval_runs`에 남긴다 (DESIGN.md 13절 6단계, 완료 기준 6-1, 6-3).

`python -m app.worker.cli eval`이 부른다. `tests/e2e`의 docker 테스트 전부(케이스 파일 하네스,
도구를 끈 평가, 비교 하네스)를 pytest 하위 프로세스로 돌리고, junit 결과와 도구를 끈 평가의 JSON을
읽어 한 줄로 저장한다. 기본은 케이스의 가짜 모델이고, `real_model`이면 하네스가 로그인된 claude를
부른다(비용이 든다). 하위 프로세스에는 docker·gh·claude가 쓰는 최소 환경만 넘긴다.
"""

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from sqlalchemy import Engine

from app.shared.db_pure import EvalRun
from app.worker.evaluation import eval_run_pure
from app.worker.store import state

# 하위 프로세스에 넘기는 환경. 있을 때만 넘긴다
PASSED_ENV: Final = (
    "HOME", "USER", "PATH", "TMPDIR", "LANG", "LC_ALL", "CLAUDE_CONFIG_DIR",
    "DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CONTEXT", "GH_CONFIG_DIR",
)  # fmt: skip
TOOLS_OFF_OUT_ENV: Final = "PR_PROOF_TOOLS_EVAL_OUT"
REAL_MODEL_ENV: Final = "PR_PROOF_REAL_MODEL"
# 실제 모델 평가의 호출 기록이자 같은 입력의 답을 다시 쓰는 파일(`tests/e2e/model_memo.py`).
# 주지 않으면 그 실행의 임시 폴더에 두어 실행 하나 안에서만 다시 쓴다
MODEL_MEMO_ENV: Final = "PR_PROOF_MODEL_MEMO"
SUITE: Final = "tests/e2e"

Runner = Callable[..., subprocess.CompletedProcess]


class EvalError(RuntimeError):
    """평가 세트가 결과를 남기지 못했다 (pytest가 돌지 못함)."""


@dataclass(frozen=True)
class Stored:
    run_id: int
    summary: eval_run_pure.RunSummary
    baseline: EvalRun | None  # 비교한 기준선. 기준선으로 저장했거나 기준선이 없으면 None


def suite_env(environ: Mapping[str, str], out_dir: Path, real_model: bool) -> dict[str, str]:
    env = {k: environ[k] for k in PASSED_ENV if k in environ}
    env[TOOLS_OFF_OUT_ENV] = str(out_dir)
    if real_model:
        env[REAL_MODEL_ENV] = "1"
        env[MODEL_MEMO_ENV] = environ.get(MODEL_MEMO_ENV) or str(out_dir / "memo.jsonl")
    return env


def run_suite(
    repo_root: Path,
    out_dir: Path,
    *,
    real_model: bool,
    environ: Mapping[str, str] | None = None,
    runner: Runner = subprocess.run,
) -> eval_run_pure.RunSummary:
    junit = out_dir / "junit.xml"
    command = [sys.executable, "-m", "pytest", "-m", "docker", SUITE, "-q",
               "-p", "no:cacheprovider", f"--junitxml={junit}"]  # fmt: skip
    env = suite_env(os.environ if environ is None else environ, out_dir, real_model)
    runner(command, cwd=repo_root, env=env, check=False)
    if not junit.is_file():
        raise EvalError(f"pytest가 결과 파일을 남기지 않았다: {junit}")
    tools_off: dict = {}
    report = out_dir / "tools_off.json"
    if report.is_file():
        tools_off = json.loads(report.read_text()).get("by_method", {})
    return eval_run_pure.RunSummary(
        cases=eval_run_pure.parse_junit(junit.read_text()),
        tools_off=tools_off,
        models=eval_run_pure.default_models(),
        prompts=eval_run_pure.prompt_versions(),
    )


def pipeline_commit(repo_root: Path, runner: Runner = subprocess.run) -> str:
    proc = runner(["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True,
                  env={"PATH": os.environ.get("PATH", "")}, check=False)  # fmt: skip
    return proc.stdout.strip() if proc.returncode == 0 else ""


def to_row(summary: eval_run_pure.RunSummary, *, label: str, baseline: bool, commit: str,
           real_model: bool, now: datetime | None = None) -> EvalRun:  # fmt: skip
    return EvalRun(
        created_at=now or datetime.now(UTC),
        label=label,
        baseline=baseline,
        pipeline_commit=commit,
        real_model=real_model,
        passed=summary.count("passed"),
        failed=summary.count("failed"),
        skipped=summary.count("skipped"),
        cases_json=eval_run_pure.cases_json(summary.cases),
        tools_off_json=json.dumps(summary.tools_off, ensure_ascii=False),
        models_json=json.dumps(summary.models, ensure_ascii=False),
        prompts_json=json.dumps(summary.prompts, ensure_ascii=False),
    )


def from_row(row: EvalRun) -> eval_run_pure.RunSummary:
    return eval_run_pure.RunSummary(
        cases=eval_run_pure.cases_from_json(row.cases_json),
        tools_off=json.loads(row.tools_off_json),
        models=json.loads(row.models_json),
        prompts=json.loads(row.prompts_json),
    )


@contextmanager
def _out_dir(out: Path | None) -> Iterator[Path]:
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
        yield out
        return
    with tempfile.TemporaryDirectory(prefix="pr-proof-eval-") as tmp:
        yield Path(tmp)


def evaluate(
    engine: Engine,
    repo_root: Path,
    *,
    baseline: bool,
    real_model: bool,
    label: str = "",
    runner: Runner = subprocess.run,
    environ: Mapping[str, str] | None = None,
    out: Path | None = None,
) -> Stored:
    """평가 세트를 돌려 저장한다. 기준선으로 저장하지 않으면 가장 최근 기준선을 함께 돌려준다.
    out을 주면 junit·도구를 끈 평가의 JSON·호출 기록을 그 폴더에 남긴다(없으면 임시 폴더)."""
    with _out_dir(out) as folder:
        summary = run_suite(repo_root, folder, real_model=real_model, environ=environ,
                            runner=runner)  # fmt: skip
    row = to_row(summary, label=label, baseline=baseline, real_model=real_model,
                 commit=pipeline_commit(repo_root, runner))  # fmt: skip
    run_id = state.add_eval_run(engine, row)
    base = None if baseline else state.latest_baseline(engine, real_model, before_id=run_id)
    return Stored(run_id, summary, base)
