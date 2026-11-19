"""문서 규칙 위반의 모델 수정이 그래프에서 도는지 (docs/DESIGN.md 4.7, 4.8, 4.9, 5.1, 5.3 (b)).

실제 git 체크아웃(로컬 원격), 실제 모델 전용 폴더와 git apply, 실제 SQLite. 모델(claude)과 검사기
샌드박스, 테스트 실행, GitHub, 푸시만 가짜다. Docker 없음.
"""

import json
from dataclasses import replace
from pathlib import Path
from typing import Final

import pytest
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.models import model_fix as model_fix_rules
from app.models import state_keys
from app.models.job import PRJob
from app.models.repo_config import CallSettings, ModelSettings, RepoConfig, SkillsSettings
from app.models.rule import rule_id
from app.models.test_run import ReportSummary
from app.worker import graph, repo, update
from app.worker.ai import claude_cli
from app.worker.ai.claude_cli import AIErrorKind, ClaudeResult
from app.worker.markdown import inline_code
from app.worker.proposal import FORK_NO_PR

from .helpers import PASSED, bodies, use_runner
from .test_doc_rules_flow import (  # noqa: F401
    BASE_FILES,
    PRINT_RULE,
    TWO_PRINTS,
    FakeModel,
    _covered_tests,
    doc_env,
    make_remote,
    print_rule,
    rule_answer,
    use_config,
    use_model,
)

RID: Final = rule_id("README.md", PRINT_RULE)
BRANCH: Final = "pipeline/pr-1-fix-model"
# v1 lint·static을 끄고 기본값(바로 수정)으로 둔다. 문서 규칙 경로만 본다
AUTO: Final = RepoConfig(skills=SkillsSettings(enabled=[]))
FIX_LINE_2: Final = {"orders/a.py": [("print(x)\n", "log(x)\n")]}


def _explanation(*changes: tuple[str, int], rule: str = RID) -> str:
    return json.dumps({
        "summary": "print를 로거 호출로 바꿈",
        "changes": [{"file": f, "line": n, "rule": rule, "why": "로거를 쓴다"} for f, n in changes],
    }, ensure_ascii=False)  # fmt: skip


class FakeFixer:
    """수정 작성의 가짜 claude. 회차마다 edits의 다음 편집을 모델 폴더에 적용하고 answers의 다음
    답을 낸다 (모자라면 마지막 것). 받은 인자를 남긴다."""

    def __init__(self, edits: list[dict[str, list[tuple[str, str]]]], answers: list[str]) -> None:
        self.edits, self.answers = edits, answers
        self.calls: list[dict] = []

    def __call__(
        self, work: Path, prompt: str, *, model: str, max_budget_usd: float,
        settings: Path | None = None, system_prompt: str | None = None,
    ) -> ClaudeResult:  # fmt: skip
        n = len(self.calls)
        self.calls.append({"prompt": prompt, "system_prompt": system_prompt, "model": model,
                           "files": sorted(p.relative_to(work).as_posix()
                                           for p in work.rglob("*") if p.is_file())})  # fmt: skip
        for file, swaps in self.edits[min(n, len(self.edits) - 1)].items():
            path = work / file
            text = path.read_text()
            for old, new in swaps:
                text = text.replace(old, new, 1)
            path.write_text(text)
        return ClaudeResult(
            ok=True, error=None, cost_usd=0.0, num_turns=2, permission_denials=(),
            text=self.answers[min(n, len(self.answers) - 1)], model_id="claude-haiku-5-5",
            input_tokens=1500, output_tokens=120,
        )  # fmt: skip


def first_fix_then_skip(ids: list[str]) -> list[dict]:
    """첫 위반은 fix, 나머지는 skip (2-9)."""
    return [{"id": ids[0], "action": "fix", "why": "로거로 바꿀 수 있다"}] + [
        {"id": i, "action": "skip", "why": "출력이 기능이다"} for i in ids[1:]
    ]


@pytest.fixture
def fix_env(doc_env, tmp_path: Path):  # noqa: F811
    """doc_env에 바로 수정하는 설정, 커버된 테스트, 가짜 푸시를 붙인다."""
    use_config(doc_env, AUTO)
    _covered_tests(doc_env, tmp_path, {"orders/a.py": [1, 2, 3]})
    pushes: list[dict] = []

    def fake_push(worktree, paths, message, branch, author):
        pushes.append({"paths": list(paths), "branch": branch, "message": message,
                       "diff": repo.head_diff(worktree)})  # fmt: skip
        return "c" * 40

    doc_env["monkeypatch"].setattr(repo, "commit_and_push", fake_push)
    doc_env["pushes"] = pushes
    return doc_env


def use_fixer(env, fixer: FakeFixer) -> FakeFixer:
    env["monkeypatch"].setattr(claude_cli, "run_claude", fixer)
    return fixer


def run(env, tmp_path: Path, pr: dict[str, str] = TWO_PRINTS, base: dict = BASE_FILES):
    remote = tmp_path / "remote"
    base_sha, head = make_remote(remote, base, pr)
    env["gh"].head, env["gh"].base = head, base_sha
    env["redis"].set(state_keys.latest_head("o/r", 1), head)
    job = PRJob(repo="o/r", pr_number=1, kind="update", head_sha=head, base_sha=base_sha,
                source="webhook")  # fmt: skip
    deps = env["deps"]
    return graph.run_flow(job, update._context(deps, deps.store.start_run(job), None))


def _comment(env) -> str:
    return bodies(env["gh"])[-1]


def test_fix_decided_violation_reaches_a_fix_pull_request(fix_env, tmp_path: Path) -> None:
    """2-1(흐름), 2-9: fix로 정한 위반만 고쳐 수정안 PR로 올리고, skip한 위반은 이유와 함께 코멘트에
    남고 수정안에 들어가지 않는다."""
    use_model(fix_env, FakeModel(rule_answer(print_rule()), decide=first_fix_then_skip))
    fixer = use_fixer(fix_env, FakeFixer([FIX_LINE_2], [_explanation(("orders/a.py", 2))]))
    state = run(fix_env, tmp_path)

    fix = state["model_fix"]
    first, second = state["doc_rules"].violations
    assert fix.ready and fix.targets == (first.id,) and fix.attempts == 1
    assert fix.verification == model_fix_rules.Verification(
        tests=9, resolved=1, rules_rechecked=1, lint_rechecked=False, files=1, lines=2
    )
    ((push),) = fix_env["pushes"]
    assert push["paths"] == ["orders/a.py"] and push["branch"] == BRANCH
    assert "-print(x)\n+log(x)" in push["diff"] and "-print(x + 1)" not in push["diff"]
    assert (
        push["message"]
        == f"[pipeline] model: #1 문서 규칙 위반 1건 수정\n\n원본 커밋 {state['job'].head_sha}"
    )
    ((number, pull),) = fix_env["gh"].pulls.items()
    assert (pull["base"], pull["head"]) == ("feature", BRANCH)
    assert pull["title"] == "[pipeline] #1 문서 규칙 위반 1건 수정 (모델)"
    body = pull["body"]
    assert f"- `{RID}` `` {PRINT_RULE} `` · 출처: `README.md > 코드 규칙 > 출력`" in body
    assert "  - `orders/a.py:2`" in body and "orders/a.py:3" not in body
    assert "수정 판단이 skip한 위반 1건은 이 수정안에 넣지 않았습니다" in body
    assert "- 테스트: 수정 전후 테스트 9개가 같은 결과로 통과" in body
    assert "- 규칙 재검사: 대상 위반 1건 해소, 새 위반 없음 (검사기 1개를 다시 돌림)" in body
    assert "**모델의 설명** (모델이 쓴 글입니다)" in body and "print를 로거 호출로 바꿈" in body

    comment = _comment(fix_env)
    assert "**수정안:** 위반 1건을 고쳤습니다." in comment
    assert f"수정안 PR #{number}을 열었습니다." in comment
    assert f"  - `orders/a.py:3` · skip (모델: {inline_code('출력이 기능이다')})" in comment
    assert "아직 만들지 않습니다" not in comment
    assert "수정 작성: 모델 호출 1회, 회차 1 · `claude-haiku-5-5`" in comment
    # 지시는 시스템 프롬프트로, 규칙 원문과 위반은 자료로. 모델 폴더에는 worktree의 파일이 있다
    (call,) = fixer.calls
    assert call["system_prompt"] == model_fix_rules.SYSTEM_PROMPT and call["model"] == "haiku"
    assert PRINT_RULE in call["prompt"] and "orders/a.py:2" in call["prompt"]
    assert "orders/a.py:3" not in call["prompt"] and "orders/a.py" in call["files"]
    (row,) = [c for c in fix_env["deps"].store.model_calls("o/r", 1) if c.stage == "fix"]
    assert (row.ok, row.counted, row.input_tokens, row.output_tokens, row.model_id) == (
        True, 1, 1500, 120, "claude-haiku-5-5",
    )  # fmt: skip


def test_explanation_pointing_at_an_unchanged_line_is_dropped(fix_env, tmp_path: Path) -> None:
    """2-5: 설명 JSON이 diff에 없는 줄을 가리키면 그 회차는 검증에 떨어지고 이유가 다음 회차에
    간다. 회차를 다 쓰면 수정안 PR이 없다."""
    use_model(fix_env, FakeModel(rule_answer(print_rule()), decide=first_fix_then_skip))
    fake = _explanation(("orders/a.py", 2), ("orders/a.py", 1))
    fixer = use_fixer(fix_env, FakeFixer([FIX_LINE_2], [fake]))
    state = run(fix_env, tmp_path)

    fix = state["model_fix"]
    assert fix.status == "failed" and fix.attempts == 3 and len(fixer.calls) == 3
    assert "설명이 바뀌지 않은 줄을 가리킴: orders/a.py:1" in fix.detail
    assert "설명이 바뀌지 않은 줄을 가리킴: orders/a.py:1" in fixer.calls[1]["prompt"]
    assert fix_env["gh"].pulls == {} and fix_env["pushes"] == []
    assert "**수정안:** 수정안을 만들지 못함: 정해진 회차 안에 검증을 통과하지 못함" in _comment(
        fix_env
    )


def test_fix_that_breaks_tests_is_retried_then_dropped(fix_env, tmp_path: Path) -> None:
    """2-7(흐름): 고친 뒤 테스트가 실패하면 실패한 테스트를 다음 회차에 넘기고, 끝내 PR이 없다."""
    use_model(fix_env, FakeModel(rule_answer(print_rule()), decide=first_fix_then_skip))
    fixer = use_fixer(fix_env, FakeFixer([FIX_LINE_2], [_explanation(("orders/a.py", 2))]))
    calls = {"n": 0}
    failing = replace(PASSED, exit_code=1,
                      summary=ReportSummary(passed=8, failed=1, total=9,
                                            failed_tests=("tests/test_a.py::test_x",)))  # fmt: skip

    def runner(*a, **k):
        calls["n"] += 1
        return (
            replace(PASSED, coverage_xml=tmp_path / "coverage.xml") if calls["n"] == 1 else failing
        )

    use_runner(fix_env, runner)
    state = run(fix_env, tmp_path)
    assert state["model_fix"].status == "failed" and len(fixer.calls) == 3
    assert "실패한 테스트: tests/test_a.py::test_x" in fixer.calls[1]["prompt"]
    assert fix_env["gh"].pulls == {}


def test_change_outside_the_allowed_files_is_dropped_without_retry(fix_env, tmp_path) -> None:
    use_model(fix_env, FakeModel(rule_answer(print_rule())))
    base = {**BASE_FILES, "tests/test_a.py": "def test_a():\n    assert True\n"}
    edits = {**FIX_LINE_2, "tests/test_a.py": [("assert True", "assert 1")]}
    fixer = use_fixer(fix_env, FakeFixer([edits], [_explanation(("orders/a.py", 2))]))
    state = run(fix_env, tmp_path, base=base)
    fix = state["model_fix"]
    assert fix.status == "dropped" and len(fixer.calls) == 1
    assert "tests/test_a.py" in fix.detail and fix_env["gh"].pulls == {}
    assert "**수정안:** 수정안을 만들지 못함: 수정안을 버림" in _comment(fix_env)


def test_skipped_and_uncovered_violations_never_reach_the_fixer(fix_env, tmp_path) -> None:
    """2-7(흐름): 테스트가 지나가지 않는 위반은 표가 skip하고 모델 수정을 부르지 않는다."""
    use_model(fix_env, FakeModel(rule_answer(print_rule())))
    _covered_tests(fix_env, tmp_path, {"orders/a.py": [1]})
    fixer = use_fixer(fix_env, FakeFixer([FIX_LINE_2], [_explanation(("orders/a.py", 2))]))
    state = run(fix_env, tmp_path)
    assert "model_fix" not in state and fixer.calls == [] and fix_env["gh"].pulls == {}
    assert "**수정안:**" not in _comment(fix_env)


def test_fork_pull_request_gets_no_fix_pull_request(fix_env, tmp_path: Path) -> None:
    use_model(fix_env, FakeModel(rule_answer(print_rule()), decide=first_fix_then_skip))
    use_fixer(fix_env, FakeFixer([FIX_LINE_2], [_explanation(("orders/a.py", 2))]))
    fix_env["gh"].head_repo = "someone/r"
    state = run(fix_env, tmp_path)
    assert state["model_fix"].ready and state["proposals"]["model"].reason == FORK_NO_PR
    assert fix_env["gh"].pulls == {} and fix_env["pushes"] == []
    comment = _comment(fix_env)
    assert "수정안 PR을 만들지 않았습니다 (포크에서 온 PR" in comment
    assert "<details><summary>수정 내용</summary>" in comment and "+log(x)" in comment


def test_fix_call_cap_leaves_the_fix_unprocessed(fix_env, tmp_path: Path) -> None:
    """수정 작성은 PR 상한에서 센다. 규칙 읽기·검사기 작성·수정 판단 뒤에 남은 호출이 없으면
    부르지 않고 코멘트에 처리하지 못한 수정 건수가 나온다 (9.2)."""
    use_model(fix_env, FakeModel(rule_answer(print_rule()), decide=first_fix_then_skip))
    use_config(fix_env, AUTO.model_copy(update={"model": ModelSettings(
        calls=CallSettings(pr_max=1))}))  # fmt: skip
    fixer = use_fixer(fix_env, FakeFixer([FIX_LINE_2], [_explanation(("orders/a.py", 2))]))
    state = run(fix_env, tmp_path)
    assert state["model_fix"].status == "budget" and fixer.calls == []
    assert "모델 호출 상한에 걸려 처리하지 못함: 수정 1건" in _comment(fix_env)


def test_same_commit_again_reuses_the_saved_fix_without_calls(fix_env, tmp_path: Path) -> None:
    """수정 작성의 결과는 입력 해시로 저장한다. 같은 커밋을 다시 검사하면 모델을 부르지 않고 저장한
    diff를 적용해 같은 검증을 다시 한다 (2.2, 9.1)."""
    use_model(fix_env, FakeModel(rule_answer(print_rule()), decide=first_fix_then_skip))
    fixer = use_fixer(fix_env, FakeFixer([FIX_LINE_2], ["설명 없음"]))
    first = run(fix_env, tmp_path)
    assert first["model_fix"].status == "failed" and len(fixer.calls) == 3
    deps, job = fix_env["deps"], first["job"]
    again = graph.run_flow(job, update._context(deps, deps.store.start_run(job), None))
    assert len(fixer.calls) == 3
    assert again["model_fix"].status == "failed" and again["model_fix"].cache_hits == 3
    assert again["model_fix"].calls == 0


def test_require_approval_keeps_the_model_fix_from_running(fix_env, tmp_path: Path) -> None:
    use_model(fix_env, FakeModel(rule_answer(print_rule())))
    use_config(fix_env, AUTO.model_copy(update={"skills": SkillsSettings(
        enabled=[], fix={"require_approval": True})}))  # fmt: skip
    fixer = use_fixer(fix_env, FakeFixer([FIX_LINE_2], [_explanation(("orders/a.py", 2))]))
    state = run(fix_env, tmp_path)
    assert "model_fix" not in state and fixer.calls == []
    assert "`require_approval: true`라 문서 규칙 위반의 수정안을" in _comment(fix_env)


def test_model_outside_the_tool_list_is_an_environment_error(fix_env, tmp_path: Path) -> None:
    """도구 목록이 다르면(CLI가 바뀜) 결과를 쓰지 않고 환경 오류로 작업을 다시 시도한다."""
    use_model(fix_env, FakeModel(rule_answer(print_rule())))

    def wrong_tools(*a, **k) -> ClaudeResult:
        return ClaudeResult(ok=False, error=AIErrorKind.TOOLS, cost_usd=None, num_turns=0,
                            permission_denials=(), text="", detail="도구 목록이 다름")  # fmt: skip

    use_fixer(fix_env, wrong_tools)
    with pytest.raises(Exception, match="claude 호출을 중단함"):
        run(fix_env, tmp_path)


def test_model_fix_state_survives_checkpoint() -> None:
    fix = model_fix_rules.ModelFix(
        status="ready", targets=("v1",),
        explanation=model_fix_rules.Explanation(
            "s", (model_fix_rules.Change("a.py", 2, "R", "w"),)
        ),
        verification=model_fix_rules.Verification(9, 1, 1, False, 1, 2),
        patch_path=Path("/tmp/x.patch"),
    )  # fmt: skip
    serde = JsonPlusSerializer(allowed_msgpack_modules=list(graph._STATE_TYPES))
    restored = serde.loads_typed(serde.dumps_typed({"f": fix}))["f"]
    assert isinstance(restored.explanation.changes[0], model_fix_rules.Change)
    assert isinstance(restored.verification, model_fix_rules.Verification)
