import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest
from sqlalchemy import create_engine, inspect, text

from app.worker import cli
from app.worker.evaluation import evaluation
from app.worker.evaluation.eval_run_pure import RunSummary
from app.worker.store import state
from app.worker.store.state import Store

from ...redis_stand_ins import MemoryKeys

JUNIT_OK: Final = (
    '<testsuite><testcase classname="tests.e2e.test_rules_cases" '
    'name="test_rules_case[01-print-in-orders]" time="1"/>'
    '<testcase classname="tests.e2e.test_tools_off_eval" name="test_tools" time="2"/></testsuite>'
)
JUNIT_BROKEN: Final = (
    '<testsuite><testcase classname="tests.e2e.test_rules_cases" '
    'name="test_rules_case[01-print-in-orders]" time="1"><failure/></testcase></testsuite>'
)
BY_METHOD: Final = {"off/checker": {"recall": "8/9", "precision": "8/8"}}


class FakePytest:
    """pytest 하위 프로세스 대신 junit과 도구를 끈 평가의 JSON을 쓴다."""

    def __init__(self, junit: str | None, tools_off: dict | None = None) -> None:
        self.junit, self.tools_off = junit, tools_off
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, command, **kw):  # noqa: ANN001
        if command[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(command, 0, "abc123\n", "")
        self.calls.append((command, kw))
        junit = Path(next(a for a in command if a.startswith("--junitxml=")).split("=", 1)[1])
        out = Path(kw["env"][evaluation.TOOLS_OFF_OUT_ENV])
        if self.junit is not None:
            junit.write_text(self.junit)
        if self.tools_off is not None:
            (out / "tools_off.json").write_text(json.dumps({"by_method": self.tools_off}))
        return subprocess.CompletedProcess(command, 0, "", "")


def test_suite_runs_every_docker_test_with_a_minimal_environment(tmp_path: Path) -> None:
    runner = FakePytest(JUNIT_OK, BY_METHOD)
    environ = {"HOME": "/h", "PATH": "/bin", "GITHUB_TOKEN": "secret", "AWS_KEY": "x",
               "PR_PROOF_TOOLS_EVAL_MATHLLM": "/private/code"}  # fmt: skip
    summary = evaluation.run_suite(tmp_path, tmp_path, real_model=False, environ=environ,
                                   runner=runner)  # fmt: skip
    ((command, kw),) = runner.calls
    assert command[1:6] == ["-m", "pytest", "-m", "docker", "tests/e2e"]
    assert kw["cwd"] == tmp_path
    assert kw["env"] == {"HOME": "/h", "PATH": "/bin",
                         "PR_PROOF_TOOLS_EVAL_OUT": str(tmp_path)}  # fmt: skip
    assert [c.case for c in summary.cases] == ["rules/01-print-in-orders",
                                               "test_tools_off_eval::test_tools"]  # fmt: skip
    assert summary.tools_off == BY_METHOD
    assert summary.prompts["fix"] and summary.models["light"] == "haiku"


def test_real_model_is_passed_only_when_asked(tmp_path: Path) -> None:
    runner = FakePytest(JUNIT_OK)
    summary = evaluation.run_suite(tmp_path, tmp_path, real_model=True, environ={},
                                   runner=runner)  # fmt: skip
    env = runner.calls[0][1]["env"]
    assert env["PR_PROOF_REAL_MODEL"] == "1"
    # 호출 기록은 주지 않으면 그 실행의 임시 폴더에 둔다 (실행 하나 안에서만 같은 답을 다시 쓴다)
    assert env["PR_PROOF_MODEL_MEMO"] == str(tmp_path / "memo.jsonl")
    assert summary.tools_off == {}  # 도구를 끈 평가가 결과를 남기지 않았으면 비어 있다


def test_memo_file_is_passed_through_only_for_the_real_model(tmp_path: Path) -> None:
    runner = FakePytest(JUNIT_OK)
    environ = {"PR_PROOF_MODEL_MEMO": "/keep/memo.jsonl"}
    evaluation.run_suite(tmp_path, tmp_path, real_model=True, environ=environ, runner=runner)
    evaluation.run_suite(tmp_path, tmp_path, real_model=False, environ=environ, runner=runner)
    real, fake = (kw["env"] for _, kw in runner.calls)
    assert real["PR_PROOF_MODEL_MEMO"] == "/keep/memo.jsonl"
    assert "PR_PROOF_MODEL_MEMO" not in fake


def test_no_junit_means_the_suite_did_not_run(tmp_path: Path) -> None:
    with pytest.raises(evaluation.EvalError):
        evaluation.run_suite(tmp_path, tmp_path, real_model=False, environ={},
                             runner=FakePytest(None))  # fmt: skip


def test_baseline_then_run_compares_against_the_same_kind(tmp_path: Path) -> None:
    engine = state.open_engine(tmp_path / "pipeline.db")
    first = evaluation.evaluate(engine, tmp_path, baseline=True, real_model=False, label="base",
                                runner=FakePytest(JUNIT_OK, BY_METHOD), environ={})  # fmt: skip
    assert first.baseline is None
    real = evaluation.evaluate(engine, tmp_path, baseline=False, real_model=True,
                               runner=FakePytest(JUNIT_OK), environ={})  # fmt: skip
    assert real.baseline is None  # 가짜 모델의 기준선은 실제 모델 실행의 기준선이 아니다
    later = evaluation.evaluate(engine, tmp_path, baseline=False, real_model=False,
                                runner=FakePytest(JUNIT_BROKEN, BY_METHOD), environ={})  # fmt: skip
    assert later.baseline is not None and later.baseline.id == first.run_id
    assert later.baseline.label == "base" and later.baseline.pipeline_commit == "abc123"
    assert (later.baseline.passed, later.baseline.failed) == (2, 0)
    assert evaluation.from_row(later.baseline) == first.summary


def test_cli_eval_prints_the_comparison(tmp_path: Path, capsys) -> None:
    db = tmp_path / "pipeline.db"
    assert cli.evaluate(db, baseline=True, real_model=False, label="",
                        runner=FakePytest(JUNIT_OK, BY_METHOD)) == 0  # fmt: skip
    assert "eval_runs #1 (기준선, 가짜 모델)" in capsys.readouterr().out
    assert cli.evaluate(db, baseline=False, real_model=False, label="",
                        runner=FakePytest(JUNIT_BROKEN, BY_METHOD)) == 0  # fmt: skip
    out = capsys.readouterr().out
    assert "eval_runs #2 (실행, 가짜 모델)" in out
    assert "새로 실패 1개: rules/01-print-in-orders" in out
    assert "없어진 케이스 1개: test_tools_off_eval::test_tools" in out
    assert cli.evaluate(db, baseline=False, real_model=False, label="",
                        runner=FakePytest(None)) == 1  # fmt: skip
    assert "평가를 남기지 못함" in capsys.readouterr().err


def test_out_folder_keeps_the_results_and_its_memo_is_reused(tmp_path: Path) -> None:
    out = tmp_path / "run1"
    runner = FakePytest(JUNIT_OK, BY_METHOD)
    assert cli.evaluate(tmp_path / "p.db", baseline=True, real_model=True, label="", out=out,
                        runner=runner) == 0  # fmt: skip
    assert (out / "junit.xml").is_file() and (out / "tools_off.json").is_file()
    assert runner.calls[0][1]["env"]["PR_PROOF_MODEL_MEMO"] == str(out / "memo.jsonl")
    # 기본(out 없음)은 임시 폴더라 실행이 끝나면 남지 않는다
    evaluation.evaluate(state.open_engine(tmp_path / "p.db"), tmp_path, baseline=False,
                        real_model=False, runner=runner, environ={})  # fmt: skip
    gone = Path(runner.calls[1][1]["env"]["PR_PROOF_TOOLS_EVAL_OUT"])
    assert not gone.exists()


def test_cli_eval_without_a_baseline_says_so(tmp_path: Path, capsys) -> None:
    assert cli.evaluate(tmp_path / "p.db", baseline=False, real_model=False, label="",
                        runner=FakePytest(JUNIT_OK)) == 0  # fmt: skip
    assert "비교할 기준선이 없음" in capsys.readouterr().out


def test_existing_database_gets_eval_runs_and_keeps_its_rows(tmp_path: Path) -> None:
    """6단계 전의 데이터베이스(`eval_runs` 없음)를 그대로 연다. 있던 줄은 남고 새 테이블이
    생긴다."""
    path = tmp_path / "pipeline.db"
    Store(path, MemoryKeys())  # 지금의 테이블을 만든 뒤 eval_runs만 지운다
    old = create_engine(f"sqlite:///{path}")
    with old.begin() as conn:
        conn.execute(text("DROP TABLE eval_runs"))
        conn.execute(
            text(
                "INSERT INTO commandrecord (comment_id, repo, pr_number, command, result,"
                " handled_at) VALUES (5, 'o/r', 1, '/fix', 'ok', '2026-11-01')"
            )
        )
    old.dispose()

    store = Store(path, MemoryKeys())
    assert "eval_runs" in inspect(store.engine).get_table_names()
    assert store.command_handled(5)
    now = datetime(2026, 11, 20, tzinfo=UTC)
    row = evaluation.to_row(RunSummary(cases=()), label="", baseline=True, commit="",
                            real_model=False, now=now)  # fmt: skip
    assert state.add_eval_run(store.engine, row) == 1
