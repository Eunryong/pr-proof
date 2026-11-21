"""출구 검사가 만든 테스트를 받아들이거나 빼는지 그래프에서 본다 (docs/DESIGN.md 4.8의 6번, 4.9).

모델을 부르지 않는다. 수정 작성의 결과는 저장된 수정(`model_cache`)으로 넣는다. 운영에서 같은 입력의
수정 작성을 다시 쓸 때와 같은 길이다. 테스트 결과는 샌드박스 대신 report.json과 coverage.json을 직접
써 넣는다. 규칙 읽기와 수정 판단은 이 폴더의 다른 흐름 테스트가 쓰는 답을 그대로 쓴다.
"""

import json
from pathlib import Path
from typing import Final

from app.worker.fix import model_fix_pure as model_fix_rules
from app.worker.fix.model_fix_pure import Written
from app.worker.repo_config_pure import FixSettings, RepoConfig
from app.worker.test_run_pure import PytestRun, ReportSummary

from ..helpers import use_runner
from ..rules.test_doc_rules_flow import (  # noqa: F401
    _covered_tests,
    doc_env,
    print_rule,
    rule_answer,
    use_config,
    use_model,
)
from .test_model_fix_flow import (  # noqa: F401
    RID,
    FakeModel,
    _comment,
    first_fix_then_skip,
    fix_env,
    run,
)

TEST_FILE: Final = "tests/test_pipeline_orders.py"
TEST_BODY: Final = "import orders.a\n\n\ndef test_prints():\n    assert orders.a.x == 1\n"
EXISTING: Final = "tests/test_a.py"


def _patch(test_body: str = TEST_BODY) -> bytes:
    lines = test_body.splitlines()
    added = "".join(f"+{line}\n" for line in lines)
    return (
        "diff --git a/orders/a.py b/orders/a.py\n--- a/orders/a.py\n+++ b/orders/a.py\n"
        "@@ -1,3 +1,3 @@\n x = 1\n-print(x)\n+log(x)\n print(x + 1)\n"
        f"diff --git a/{TEST_FILE} b/{TEST_FILE}\nnew file mode 100644\n--- /dev/null\n"
        f"+++ b/{TEST_FILE}\n@@ -0,0 +1,{len(lines)} @@\n{added}"
    ).encode()


def _answer(**extra) -> str:
    return json.dumps({"summary": "print를 로거로", "changes": [
        {"file": "orders/a.py", "rule": RID, "why": "로거를 쓴다"}], **extra})  # fmt: skip


def use_stored_fix(env, patch: bytes, answer: str) -> None:
    """첫 회차의 수정 작성 키에 저장된 수정을 둔다. 모델을 부르지 않고 그 수정을 적용한다."""
    real = model_fix_rules.write_key
    seeded: list[str] = []

    def key(*args, **kwargs) -> str:
        made = real(*args, **kwargs)
        if not seeded:
            seeded.append(made)
            env["deps"].store.save_result(
                made, stage="fix", model="stored", prompt_version=model_fix_rules.PROMPT_VERSION,
                result_json=model_fix_rules.written_to_json(Written(patch, answer)),
            )  # fmt: skip
        return made

    env["monkeypatch"].setattr(model_fix_rules, "write_key", key)


def use_tests(env, tmp_path: Path, *, before: str, after: str = "passed") -> list[str]:
    """테스트 단계는 있던 테스트 9개가 통과하고 위반 줄(2)을 실행하지 않은 결과. 줄마다 실행한
    테스트를 보는 실행은 고치기 전(따로 된 worktree)과 고친 뒤의 결과를 파일로 남긴다."""
    _covered_tests(env, tmp_path, {"orders/a.py": [1]})
    baseline = tmp_path / "coverage.xml"
    seen: list[str] = []

    def runner(worktree: Path, **kw) -> PytestRun:
        if not kw.get("contexts"):
            return PytestRun(exit_code=0, summary=ReportSummary(passed=9, total=9),
                             coverage_xml=baseline)  # fmt: skip
        fixed = "log(x)" in (worktree / "orders/a.py").read_text()
        has_test = (worktree / TEST_FILE).is_file()
        outcome = (after if fixed else before) if has_test else None
        seen.append(f"{'after' if fixed else 'before'}:{outcome}")
        tests = [{"nodeid": f"{EXISTING}::test_{i}", "outcome": "passed"} for i in range(9)]
        if outcome is not None:
            tests.append({"nodeid": f"{TEST_FILE}::test_prints", "outcome": outcome})
        failed = sum(1 for t in tests if t["outcome"] == "failed")
        out = worktree / ".pipeline-out"
        out.mkdir(exist_ok=True)
        (out / "report.json").write_text(json.dumps({"tests": tests}))
        contexts = {"1": [f"{EXISTING}::test_0|run"]}
        if outcome is not None:
            contexts["2"] = [f"{TEST_FILE}::test_prints|run"]
        (out / "coverage.json").write_text(json.dumps({"files": {"orders/a.py": {
            "executed_lines": [1, 2] if outcome else [1], "missing_lines": [] if outcome else [2],
            "contexts": contexts}}}))  # fmt: skip
        return PytestRun(
            exit_code=1 if failed else 0,
            summary=ReportSummary(passed=len(tests) - failed, failed=failed, total=len(tests),
                                  failed_tests=tuple(t["nodeid"] for t in tests
                                                     if t["outcome"] == "failed")),
            report_path=out / "report.json",
        )  # fmt: skip

    use_runner(env, runner)
    return seen


def test_a_generated_test_that_meets_every_condition_goes_up(fix_env, tmp_path):  # noqa: F811
    use_model(fix_env, FakeModel(rule_answer(print_rule()), decide=first_fix_then_skip))
    use_stored_fix(fix_env, _patch(), _answer())
    seen = use_tests(fix_env, tmp_path, before="passed")
    state = run(fix_env, tmp_path)

    fix = state["model_fix"]
    assert fix.ready, fix.detail
    (test,) = fix.generated
    assert (test.file, test.accepted, test.before, test.after) == (
        TEST_FILE, True, "passed", "passed")  # fmt: skip
    assert [r.status for r in fix.line_runs] == ["generated"]
    assert seen == ["before:passed", "after:passed"]
    assert fix.verification.files == 1 and fix.verification.lines == 2  # 테스트 줄은 따로 센다
    ((push),) = fix_env["pushes"]
    assert sorted(push["paths"]) == ["orders/a.py", TEST_FILE]
    stored = fix_env["deps"].store.fix_attempt(fix.attempt_id)
    assert f"+++ b/{TEST_FILE}" in stored.patch  # 전달은 이 패치를 적용해 테스트 파일까지 올린다
    ((_, pull),) = fix_env["gh"].pulls.items()
    assert "만든 테스트가 실행함" in pull["body"] and "만든 테스트 1개" in pull["body"]
    assert "만든 테스트가 실행함" in _comment(fix_env)


def test_a_test_failing_before_the_fix_is_left_out(fix_env, tmp_path):  # noqa: F811
    use_model(fix_env, FakeModel(rule_answer(print_rule()), decide=first_fix_then_skip))
    reason = {"file": "orders/a.py", "rule": RID, "why": "모듈을 불러올 때 출력하는 줄"}
    use_stored_fix(fix_env, _patch(), _answer(untested=[reason]))
    use_tests(fix_env, tmp_path, before="failed")
    state = run(fix_env, tmp_path)

    fix = state["model_fix"]
    assert fix.ready, fix.detail
    (test,) = fix.generated
    assert (test.accepted, test.rejected, test.after) == (False, "fails_before", None)
    assert [(r.status, r.why) for r in fix.line_runs] == [("none", "모듈을 불러올 때 출력하는 줄")]
    ((push),) = fix_env["pushes"]
    assert push["paths"] == ["orders/a.py"] and TEST_FILE not in push["diff"]
    ((_, pull),) = fix_env["gh"].pulls.items()
    assert "고치기 전 코드에서 실패함" in pull["body"] and TEST_FILE in pull["body"]
    assert "있던 테스트도 만든 테스트도 이 줄을 실행하지 않음" in pull["body"]
    assert "모듈을 불러올 때 출력하는 줄" in pull["body"]


def test_an_empty_test_is_left_out(fix_env, tmp_path):  # noqa: F811
    use_model(fix_env, FakeModel(rule_answer(print_rule()), decide=first_fix_then_skip))
    use_stored_fix(fix_env, _patch("def test_prints():\n    pass\n"), _answer())
    use_tests(fix_env, tmp_path, before="passed")
    fix = run(fix_env, tmp_path)["model_fix"]
    assert fix.ready and fix.generated[0].rejected == "empty"


def test_a_test_broken_by_the_fix_sends_the_fix_back(fix_env, tmp_path):  # noqa: F811
    """고치기 전에 통과한 테스트가 고친 뒤 실패하면 수정이 동작을 바꾼 것이다. 테스트를 빼고 올리지
    않는다."""
    use_config(fix_env, RepoConfig(fix=FixSettings(max_retries=1)))
    use_model(fix_env, FakeModel(rule_answer(print_rule()), decide=first_fix_then_skip))
    use_stored_fix(fix_env, _patch(), _answer())
    use_tests(fix_env, tmp_path, before="passed", after="failed")
    fix = run(fix_env, tmp_path)["model_fix"]
    assert fix.status == "failed"
    assert "만든 테스트 1개가 고치기 전에는 통과했는데 고친 뒤 실패함" in fix.detail
    assert fix_env["gh"].pulls == {} and fix_env["pushes"] == []


def test_a_pr_that_needs_no_test_keeps_its_calls_and_text(fix_env, tmp_path):  # noqa: F811
    """있던 테스트가 위반 줄을 실행하면 만든 테스트가 없고, 테스트는 한 번만 더 돈다."""
    use_model(fix_env, FakeModel(rule_answer(print_rule()), decide=first_fix_then_skip))
    patch = _patch().split(b"diff --git a/tests")[0]
    use_stored_fix(fix_env, patch, _answer())
    calls: list[dict] = []
    covered = tmp_path / "covered.xml"
    covered.write_text('<coverage><packages><package><classes><class filename="orders/a.py">'
                       '<lines><line number="2" hits="1"/></lines></class></classes></package>'
                       "</packages></coverage>")  # fmt: skip

    def runner(worktree, **kw):
        calls.append(kw)
        return PytestRun(exit_code=0, summary=ReportSummary(passed=9, total=9),
                         coverage_xml=covered)  # fmt: skip

    use_runner(fix_env, runner)
    fix = run(fix_env, tmp_path)["model_fix"]
    assert fix.ready and fix.generated == ()
    assert [r.status for r in fix.line_runs] == ["existing"]
    assert len(calls) == 2  # 테스트 단계 한 번, 출구 검사 한 번
    body = fix_env["gh"].pulls[next(iter(fix_env["gh"].pulls))]["body"]
    assert "- 만든 테스트" not in body and "- 있던 테스트가 실행함 (1건)" in body
