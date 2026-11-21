"""문서 규칙 위반의 모델 수정이 그래프에서 도는지 (docs/DESIGN.md 4.7, 4.8, 4.9, 5.1, 5.3 (b)).

실제 git 체크아웃(로컬 원격), 실제 모델 전용 폴더와 git apply, 실제 SQLite. 모델(claude)과 검사기
샌드박스, 테스트 실행, GitHub, 푸시만 가짜다. Docker 없음.
"""

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from shared import state_keys_pure as state_keys
from shared.db_pure import ModelCall
from shared.job_pure import PRJob
from worker import graph, main, repo, retry, update
from worker.ai import claude_cli, workdir
from worker.ai.claude_cli import AIErrorKind, ClaudeResult
from worker.fix import model_fix_pure as model_fix_rules
from worker.fix.proposal import FORK_NO_PR
from worker.github import GitHubError
from worker.markdown import inline_code
from worker.repo_config_pure import (
    CallSettings,
    FixSettings,
    ModelSettings,
    RepoConfig,
)
from worker.rule_pure import rule_id
from worker.test_run_pure import ReportSummary

from ..helpers import PASSED, bodies, use_runner
from ..rules.test_doc_rules_flow import (  # noqa: F401
    BASE_FILES,
    PRINT_RULE,
    TWO_PRINTS,
    _covered_tests,
    doc_env,
    make_remote,
    print_rule,
    rule_answer,
    use_config,
    use_model,
)
from ..rules.test_doc_rules_flow import FakeModel as DocFakeModel
from ..test_doc_rules_report import _outside_code
from ..test_github import is_reply

RID: Final = rule_id("README.md", PRINT_RULE)
BRANCH: Final = "pipeline/pr-1-fix-model"
# 기본값(바로 수정). base에 ruff 설정이 없어 문서 규칙 경로만 본다
AUTO: Final = RepoConfig()
FIX_LINE_2: Final = {"orders/a.py": [("print(x)\n", "log(x)\n")]}


def _explanation(*files: str, rule: str = RID) -> str:
    """수정 작성의 마지막 답: 파일마다 (파일, 규칙) 항목 하나. 줄 번호는 파이프라인이 채운다."""
    return json.dumps({
        "summary": "print를 로거 호출로 바꿈",
        "changes": [{"file": f, "rule": rule, "why": "로거를 쓴다"} for f in files],
    }, ensure_ascii=False)  # fmt: skip


class FakeFixer:
    """수정 작성의 가짜 claude. 회차마다 edits의 다음 편집을 모델 폴더에 적용하고 answers의 다음
    답을 낸다 (모자라면 마지막 것). 받은 인자를 남긴다."""

    def __init__(self, edits: list[dict[str, list[tuple[str, str]]]], answers: list[str]) -> None:
        self.edits, self.answers = edits, answers
        self.calls: list[dict] = []

    def __call__(
        self, work: Path, prompt: str, *, model: str, max_budget_usd: float,
        settings: Path | None = None, system_prompt: str | None = None, **_: object,
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
    return [{"id": ids[0], "action": "fix", "reason": "", "why": "로거로 바꿀 수 있다"}] + [
        {"id": i, "action": "skip", "reason": "changes_behavior", "why": "출력이 기능이다"}
        for i in ids[1:]
    ]


def fix_all(ids: list[str]) -> list[dict]:
    return [{"id": i, "action": "fix", "reason": "", "why": "고칠 수 있다"} for i in ids]


class FakeModel(DocFakeModel):
    """이 파일의 수정 판단은 기본으로 모두 fix다 (수정 작성을 본다)."""

    def __init__(self, rules, checker=None, decide=None, tool=None) -> None:
        super().__init__(rules, checker, decide or fix_all, tool)


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
    fixer = use_fixer(fix_env, FakeFixer([FIX_LINE_2], [_explanation("orders/a.py")]))
    state = run(fix_env, tmp_path)

    fix = state["model_fix"]
    first, second = state["doc_rules"].violations
    assert fix.ready and fix.targets == (first.id,) and fix.attempts == 1
    assert fix.verification == model_fix_rules.Verification(
        tests=9, resolved=1, rules_rechecked=1, files=1, lines=2
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
    assert "  - `orders/a.py` 줄 2" in body and "orders/a.py:3" not in body
    assert "- 있던 테스트가 실행함 (1건): `orders/a.py` 줄 2" in body
    assert "파이프라인이 고치지 않은 위반 1건은 이 수정안에 넣지 않았습니다" in body
    assert "실행하지 않음" not in body and "만든 테스트 " not in body
    assert "- 테스트: 있던 테스트 9개가 수정 전후 같은 결과로 통과" in body
    assert "- 규칙 재검사: 대상 위반 1건 해소, 새 위반 없음 (도구·검사기 1개를 다시 돌림)" in body
    assert "**모델의 설명** (모델이 쓴 글입니다)" in body and "print를 로거 호출로 바꿈" in body

    comment = _comment(fix_env)
    assert "**모델 수정안:** 위반 1건을 고쳤습니다." in comment
    assert f"수정안 PR #{number}을 열었습니다." in comment
    assert "  직접 고쳐야 함 1건 (고치면 동작이 바뀜 1건)" in comment
    assert f"  - `orders/a.py:3` · 고치면 동작이 바뀜: {inline_code('출력이 기능이다')}" in comment
    fixed = "  - `orders/a.py:2` · 고칠 대상 (모델 수정: `로거로 바꿀 수 있다`) · 수정안 #900"
    assert fixed in comment
    assert "skip" not in comment
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


def test_explanation_pointing_at_an_unchanged_file_is_dropped(fix_env, tmp_path: Path) -> None:
    """2-5(설명의 새 모양): 설명이 바뀌지 않은 파일을 가리키면 그 회차는 검증에 떨어지고 이유가 다음
    회차에 간다. 회차를 다 쓰면 수정안 PR이 없다."""
    use_model(fix_env, FakeModel(rule_answer(print_rule()), decide=first_fix_then_skip))
    fake = _explanation("orders/a.py", "orders/b.py")
    fixer = use_fixer(fix_env, FakeFixer([FIX_LINE_2], [fake]))
    state = run(fix_env, tmp_path)

    fix = state["model_fix"]
    assert fix.status == "failed" and fix.attempts == 3 and len(fixer.calls) == 3
    assert "설명이 수정과 맞지 않음: 바뀌지 않은 파일을 가리킨 항목 1건" in fix.detail
    assert "설명이 바뀌지 않은 파일을 가리킴: orders/b.py" in fixer.calls[1]["prompt"]
    assert fix_env["gh"].pulls == {} and fix_env["pushes"] == []
    assert (
        "**모델 수정안:** 수정안을 만들지 못함: 정해진 회차 안에 검증을 통과하지 못함"
        in _comment(fix_env)
    )


def test_fix_without_a_note_for_its_rule_is_retried(fix_env, tmp_path: Path) -> None:
    """대상 위반의 줄을 바꾼 덩어리에 그 규칙의 설명이 없으면 다음 회차에 파일, 규칙, 줄이 간다."""
    use_model(fix_env, FakeModel(rule_answer(print_rule()), decide=first_fix_then_skip))
    fixer = use_fixer(fix_env, FakeFixer([FIX_LINE_2], [_explanation("orders/a.py", rule="R-x")]))
    state = run(fix_env, tmp_path)

    assert state["model_fix"].status == "failed"
    prompt = fixer.calls[1]["prompt"]
    assert f"설명이 없는 변경: orders/a.py 규칙 {RID} (고친 뒤 줄 2)" in prompt
    assert "설명의 규칙 id가 고칠 규칙이 아님: orders/a.py 규칙 R-x" in prompt
    assert "설명이 없는 파일: orders/a.py" in prompt


def test_old_answer_with_wrong_line_numbers_still_gets_the_diff_lines(fix_env, tmp_path) -> None:
    """옛 모양의 답(덩어리마다 line)도 받는다. 모델이 센 줄은 쓰지 않고 diff의 줄에 리뷰를 단다."""
    use_model(fix_env, FakeModel(rule_answer(print_rule()), decide=first_fix_then_skip))
    old = json.dumps({"summary": "s", "changes": [
        {"file": "orders/a.py", "line": 7, "rule": RID, "why": "로거"},
        {"file": "orders/a.py", "line": 1, "rule": RID, "why": "두 번째 항목"}]})  # fmt: skip
    use_fixer(fix_env, FakeFixer([FIX_LINE_2], [old]))
    state = run(fix_env, tmp_path)

    fix = state["model_fix"]
    assert fix.status == "ready"
    assert fix.explanation == model_fix_rules.Explanation(
        "s", (model_fix_rules.Change("orders/a.py", 2, RID, "로거"),)
    )


def test_fix_that_breaks_tests_is_retried_then_dropped(fix_env, tmp_path: Path) -> None:
    """2-7(흐름): 고친 뒤 테스트가 실패하면 실패한 테스트를 다음 회차에 넘기고, 끝내 PR이 없다."""
    use_model(fix_env, FakeModel(rule_answer(print_rule()), decide=first_fix_then_skip))
    fixer = use_fixer(fix_env, FakeFixer([FIX_LINE_2], [_explanation("orders/a.py")]))
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
    fixer = use_fixer(fix_env, FakeFixer([edits], [_explanation("orders/a.py")]))
    state = run(fix_env, tmp_path, base=base)
    fix = state["model_fix"]
    assert fix.status == "dropped" and len(fixer.calls) == 1
    assert "tests/test_a.py" in fix.detail and fix_env["gh"].pulls == {}
    assert "**모델 수정안:** 수정안을 만들지 못함: 수정안을 버림" in _comment(fix_env)


def test_line_no_test_runs_is_fixed_and_labelled(fix_env, tmp_path) -> None:
    """2026-11-21 (11.1), 4.9: 테스트가 실행하지 않는 줄의 위반도 고치고, 수정안 PR 본문과 결과
    코멘트가 있던 테스트도 만든 테스트도 그 줄을 실행하지 않는다고 적는다. 2026-11-22에 "이 줄을
    실행하는 테스트가 없음"을 지웠다(테스트 자체가 없다고 읽혔다)."""
    use_model(fix_env, FakeModel(rule_answer(print_rule()), decide=first_fix_then_skip))
    _covered_tests(fix_env, tmp_path, {"orders/a.py": [1]})
    fixer = use_fixer(fix_env, FakeFixer([FIX_LINE_2], [_explanation("orders/a.py")]))
    state = run(fix_env, tmp_path)
    assert state["model_fix"].ready and len(fixer.calls) == 1
    ((_, pull),) = fix_env["gh"].pulls.items()
    label = "- 있던 테스트도 만든 테스트도 이 줄을 실행하지 않음 (1건): `orders/a.py` 줄 2"
    assert label in pull["body"]
    assert "이 줄을 실행하는 테스트가 없음" not in pull["body"]
    comment = _comment(fix_env)
    assert "· 수정안 #900" in comment and label in comment


def test_skipped_violation_never_reaches_the_fixer(fix_env, tmp_path) -> None:
    """2-7(흐름): 모델이 고치지 않기로 한 위반은 수정 작성을 부르지 않는다."""
    skip_all = lambda ids: [{"id": i, "action": "skip", "reason": "needs_other_file",  # noqa: E731
                             "why": "orders/b.py의 이름을 바꿔야 한다"} for i in ids]  # fmt: skip
    use_model(fix_env, FakeModel(rule_answer(print_rule()), decide=skip_all))
    fixer = use_fixer(fix_env, FakeFixer([FIX_LINE_2], [_explanation("orders/a.py")]))
    state = run(fix_env, tmp_path)
    assert "model_fix" not in state and fixer.calls == [] and fix_env["gh"].pulls == {}
    comment = _comment(fix_env)
    assert "**모델 수정안:**" not in comment
    assert "  직접 고쳐야 함 2건 (다른 파일이나 공개 이름을 바꿔야 함 2건)" in comment


def test_fork_pull_request_gets_no_fix_pull_request(fix_env, tmp_path: Path) -> None:
    use_model(fix_env, FakeModel(rule_answer(print_rule()), decide=first_fix_then_skip))
    use_fixer(fix_env, FakeFixer([FIX_LINE_2], [_explanation("orders/a.py")]))
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
    fixer = use_fixer(fix_env, FakeFixer([FIX_LINE_2], [_explanation("orders/a.py")]))
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


def _command(text: str, comment_id: int) -> str:
    return PRJob(repo="o/r", pr_number=1, kind="command", command=text, comment_id=comment_id,
                 source="webhook").model_dump_json()  # fmt: skip


def _replies(env) -> list[str]:
    return [json.loads(stdin)["body"] for _, stdin in env["gh"].calls if is_reply(stdin)]


def test_require_approval_waits_and_fix_model_resumes(fix_env, tmp_path: Path) -> None:
    """`require_approval: true`면 모델 묶음은 승인을 기다리고(interrupt), `/fix model`이 그 thread를
    이어 수정안 PR까지 간다 (5.2). `/fix mechanical`은 이 묶음을 승인하지 않는다."""
    use_model(fix_env, FakeModel(rule_answer(print_rule()), decide=first_fix_then_skip))
    use_config(fix_env, AUTO.model_copy(update={"fix": FixSettings(require_approval=True)}))
    fixer = use_fixer(fix_env, FakeFixer([FIX_LINE_2], [_explanation("orders/a.py")]))
    state = run(fix_env, tmp_path)
    assert "model_fix" not in state and fixer.calls == [] and fix_env["gh"].pulls == {}
    assert "아직 만들지 않았습니다. `/fix model`을 달면 만듭니다." in _comment(fix_env)
    assert update.pending_fix(state["job"], fix_env["deps"], "model") == "awaiting"

    deps = fix_env["deps"]
    assert main.handle(_command("/fix mechanical", 70), deps) == "no_pending_fix"
    assert fixer.calls == []
    assert main.handle(_command("/fix model", 71), deps) == "fix:ready"
    assert len(fixer.calls) == 1 and list(fix_env["gh"].pulls) == [900]
    comment = _comment(fix_env)
    assert "수정안 PR #900을 열었습니다." in comment and "`/fix model`을 달면" not in comment
    assert deps.store.call_grants("o/r", 1) == 0  # 승인은 상한을 더 주지 않는다
    assert main.handle(_command("/fix model", 72), deps) == "no_pending_fix"
    assert "model 제안 PR #900이 이미 열려 있습니다" in _replies(fix_env)[-1]


def test_capped_fix_is_shown_and_fix_processes_the_rest(fix_env, tmp_path: Path) -> None:
    """2-11(흐름): `pr_max`를 3으로 낮추면 수정 판단 1회와 수정 작성 2회 뒤 상한에 걸려 처리하지
    못한 수정 건수가 코멘트에 나온다. `/fix`는 PR 상한을 한 번 더 받고 시도 세대를 올려, 저장된
    작성 결과를 쓰지 않고 가벼운 모델의 첫 회차부터 다시 고친다."""
    use_model(fix_env, FakeModel(rule_answer(print_rule()), decide=first_fix_then_skip))
    use_config(fix_env, AUTO.model_copy(update={"model": ModelSettings(
        calls=CallSettings(pr_max=3))}))  # fmt: skip
    answer = _explanation("orders/a.py")
    fixer = use_fixer(fix_env, FakeFixer([{}, {}, FIX_LINE_2], [answer]))
    state = run(fix_env, tmp_path)
    assert state["model_fix"].status == "budget" and len(fixer.calls) == 2
    comment = _comment(fix_env)
    assert "모델 호출 상한에 걸려 처리하지 못함: 수정 1건" in comment
    assert "`/fix`로 다시 돌릴 수 있습니다." in comment and fix_env["gh"].pulls == {}

    deps = fix_env["deps"]
    assert main.handle(_command("/fix", 80), deps) == "fix:ready"
    assert len(fixer.calls) == 3 and fixer.calls[-1]["model"] == "haiku"
    assert deps.store.call_grants("o/r", 1) == 1
    ((number, pull),) = fix_env["gh"].pulls.items()
    assert pull["head"] == BRANCH
    comment = _comment(fix_env)
    assert f"수정안 PR #{number}을 열었습니다." in comment
    assert "수정 작성: 모델 호출 1회, 회차 1" in comment and "처리하지 못함" not in comment
    pr_calls = [c for c in deps.store.model_calls("o/r", 1) if c.ruleset_key is None]
    assert sum(c.counted for c in pr_calls) == 4  # 판단 1 + 작성 2, `/fix` 뒤 작성 1
    # 같은 명령을 다시 처리해도(재시도) 상한을 두 번 받지 않고, 할 일이 없다고 답한다
    assert main.handle(_command("/fix", 81), deps) == "no_pending_fix"
    assert deps.store.call_grants("o/r", 1) == 1 and len(fixer.calls) == 3


def test_fix_reasks_decisions_left_by_the_call_cap(fix_env, tmp_path: Path) -> None:
    """수정 판단이 상한에 걸려 정하지 못한 위반은 `/fix`가 수정 판단부터 다시 돌려 정하고 고친다.
    테스트는 다시 돌리지 않는다."""
    model = use_model(fix_env, FakeModel(rule_answer(print_rule()), decide=first_fix_then_skip))
    use_config(fix_env, AUTO.model_copy(update={"model": ModelSettings(
        calls=CallSettings(pr_max=2))}))  # fmt: skip
    deps = fix_env["deps"]
    deps.store.add_model_call(ModelCall(  # 앞 커밋이 이 PR의 상한을 다 썼다
        repo="o/r", pr_number=1, head_sha="0" * 40, stage="decide", model="sonnet", tier="light",
        counted=2, ok=True, created_at=datetime.now(UTC),
    ))  # fmt: skip
    fixer = use_fixer(fix_env, FakeFixer([FIX_LINE_2], [_explanation("orders/a.py")]))
    tests = {"n": 0}

    def runner(*a, **k):
        tests["n"] += 1
        return replace(PASSED, coverage_xml=tmp_path / "coverage.xml")

    use_runner(fix_env, runner)
    state = run(fix_env, tmp_path)
    assert {d.reason for d in state["fix_decisions"].decisions} == {"call_limit"}
    assert "model_fix" not in state and fixer.calls == []
    assert "처리하지 못함: 수정 판단 2건. `/fix`로 다시 돌릴 수 있습니다." in _comment(fix_env)
    before = tests["n"]

    assert main.handle(_command("/fix model", 90), deps) == "fix:ready"
    assert [kind for kind, _ in model.prompts].count("decide") == 1 and len(fixer.calls) == 1
    assert tests["n"] == before + 1  # 수정 검증의 테스트만 돈다
    comment = _comment(fix_env)
    assert "수정 판단: 고칠 대상 1건, 직접 고쳐야 함 1건" in comment
    assert "수정안 PR #900을 열었습니다." in comment


class RuffAndGrep:
    """도구 설정의 규칙(ruff)은 가짜 ruff로, 문서 규칙의 검사기는 grep 샌드박스로 돌린다."""

    def __init__(self, grep) -> None:
        from .test_mechanical_flow import FakeRuff

        self.grep, self.ruff = grep, FakeRuff()

    def on_examples(self, kind, body, snippets):
        return self.grep.on_examples(kind, body, snippets)

    def on_files(self, kind, body, root, files):
        target = self.ruff if kind == "ruff" else self.grep
        return target.on_files(kind, body, root, files)

    def rule_exists(self, code: str) -> bool:
        return True

    def format_check(self, root):
        return self.ruff.format_check(root)

    def format_apply(self, root, files):
        return self.ruff.format_apply(root, files)


def test_model_rerun_while_mechanical_waits_keeps_its_approval(fix_env, tmp_path: Path) -> None:
    """기계적 수정 묶음이 승인을 기다리는 동안에도 상한에 걸린 모델 묶음은 `/fix model`로 다시 돌고,
    끝나면 기계적 수정 묶음은 다시 승인을 기다린다."""
    from worker import base_config
    from worker.rules import doc_rules
    from worker.tools_pure import RuffConfig, RuffSetup

    from ..helpers import BASE

    sandbox = RuffAndGrep(fix_env["sandbox"])
    fix_env["monkeypatch"].setattr(doc_rules, "sandbox_for", lambda *a: sandbox)
    use_model(fix_env, FakeModel(rule_answer(print_rule()), decide=first_fix_then_skip))
    config = RepoConfig(
        fix=FixSettings(require_approval=True), model=ModelSettings(calls=CallSettings(pr_max=3))
    )
    ruff = RuffSetup("0.17.5", config=RuffConfig("ruff.toml", "", "0" * 40))
    fix_env["monkeypatch"].setattr(
        base_config, "load", lambda *a, **k: replace(BASE, repo=config, ruff=ruff)
    )
    fixer = use_fixer(fix_env, FakeFixer([{}, {}, FIX_LINE_2], [_explanation("orders/a.py")]))
    pr = {**TWO_PRINTS, "orders/a.py": "import os\n" + TWO_PRINTS["orders/a.py"]}
    state = run(fix_env, tmp_path, pr=pr)
    assert "model_fix" not in state and "mechanical_fix" not in state
    deps = fix_env["deps"]
    assert update.pending_fix(state["job"], deps, "mechanical") == "awaiting"
    assert update.pending_fix(state["job"], deps, "model") == "awaiting"

    assert main.handle(_command("/fix model", 60), deps) == "fix:budget"
    assert len(fixer.calls) == 2
    assert update.pending_fix(state["job"], deps, "mechanical") == "awaiting"
    assert update.pending_fix(state["job"], deps, "model") == "rerun"

    assert main.handle(_command("/fix model", 61), deps) == "fix:ready"
    assert len(fixer.calls) == 3 and list(fix_env["gh"].pulls) == [900]
    assert update.pending_fix(state["job"], deps, "mechanical") == "awaiting"
    assert update.pending_fix(state["job"], deps, "model") is None


def test_fix_pull_request_gets_line_review_comments_once(fix_env, tmp_path: Path) -> None:
    """4.9: 설명 JSON의 changes마다 수정안 PR의 그 줄에 규칙과 이유를 리뷰 하나로 단다. 모델의 글은
    코드 스팬 밖으로 나오지 못한다. 리뷰를 단 뒤 응답을 받지 못해 전달을 다시 해도 두 번 달리지
    않는다 (5.6)."""
    use_model(fix_env, FakeModel(rule_answer(print_rule()), decide=first_fix_then_skip))
    why = "x` @team <b>로거</b> ``` [y](http://e)"
    change = {"file": "orders/a.py", "rule": RID, "why": why}
    answer = json.dumps({"summary": "s", "changes": [change]}, ensure_ascii=False)
    use_fixer(fix_env, FakeFixer([FIX_LINE_2], [answer]))
    gh = fix_env["gh"]
    real = gh._review
    lost = {"n": 1}

    def accepted_but_lost(number, method, args, stdin):
        out = real(number, method, args, stdin)
        if method == "POST" and lost["n"]:
            lost["n"] -= 1
            raise GitHubError("gh: 응답을 받지 못함")
        return out

    gh._review = accepted_but_lost
    deps = fix_env["deps"]
    base_sha, head = make_remote(tmp_path / "remote", BASE_FILES, TWO_PRINTS)
    gh.head, gh.base = head, base_sha
    job = PRJob(repo="o/r", pr_number=1, kind="update", head_sha=head, base_sha=base_sha,
                source="webhook")  # fmt: skip
    with pytest.raises(GitHubError):
        update.process_update(job, deps)
    fix_env["monkeypatch"].setattr(retry, "attempt", lambda: (2, True))
    assert update.process_update(job, deps) == "passed"

    (review,) = gh.reviews[900]
    assert review["event"] == "COMMENT" and review["body"].startswith("<!-- pr-proof:review ")
    (line,) = review["comments"]
    assert (line["path"], line["line"], line["side"]) == ("orders/a.py", 2, "RIGHT")
    assert line["body"].startswith(f"규칙 `{RID}` `` {PRINT_RULE} ``")
    assert inline_code(why) in line["body"]
    outside = _outside_code(line["body"])
    assert "@" not in outside and "<" not in outside and "](" not in outside


def test_review_refused_by_github_keeps_the_fix_pull_request(fix_env, tmp_path: Path) -> None:
    """GitHub이 리뷰의 줄을 받지 않으면(422) 수정안 PR은 그대로 두고 리뷰만 빠진다."""
    use_model(fix_env, FakeModel(rule_answer(print_rule()), decide=first_fix_then_skip))
    use_fixer(fix_env, FakeFixer([FIX_LINE_2], [_explanation("orders/a.py")]))
    fix_env["gh"].review_error = "gh api 실패: HTTP 422 Unprocessable Entity"
    state = run(fix_env, tmp_path)
    assert state["proposals"]["model"].number == 900 and fix_env["gh"].reviews == {}


def test_model_outside_the_tool_list_is_an_environment_error(fix_env, tmp_path: Path) -> None:
    """도구 목록이 다르면(CLI가 바뀜) 결과를 쓰지 않고 환경 오류로 작업을 다시 시도한다."""
    use_model(fix_env, FakeModel(rule_answer(print_rule())))

    def wrong_tools(*a, **k) -> ClaudeResult:
        return ClaudeResult(ok=False, error=AIErrorKind.TOOLS, cost_usd=None, num_turns=0,
                            permission_denials=(), text="", detail="도구 목록이 다름")  # fmt: skip

    use_fixer(fix_env, wrong_tools)
    with pytest.raises(Exception, match="claude 호출을 중단함"):
        run(fix_env, tmp_path)


def test_model_folder_git_failure_is_an_environment_error(fix_env, tmp_path: Path) -> None:
    """모델 전용 폴더를 만드는 호스트의 git이 실패하면 PR 탓이 아니므로 환경 오류로 남기고 RQ가
    다시 시도한다. 다음 시도에서 git이 돌면 수정안 PR까지 간다."""
    use_model(fix_env, FakeModel(rule_answer(print_rule()), decide=first_fix_then_skip))
    use_fixer(fix_env, FakeFixer([FIX_LINE_2], [_explanation("orders/a.py")]))
    real = workdir._snapshot_git
    broken = {"left": 1}

    def git_fails_once(git_dir, work, *args):
        if broken["left"]:
            broken["left"] -= 1
            raise workdir.WorkdirError("git init 실행 실패: 디스크가 가득 참")
        return real(git_dir, work, *args)

    fix_env["monkeypatch"].setattr(workdir, "_snapshot_git", git_fails_once)
    fix_env["monkeypatch"].setattr(retry, "attempt", lambda: (1, False))
    deps = fix_env["deps"]
    base_sha, head = make_remote(tmp_path / "remote", BASE_FILES, TWO_PRINTS)
    fix_env["gh"].head, fix_env["gh"].base = head, base_sha
    fix_env["redis"].set(state_keys.latest_head("o/r", 1), head)
    job = PRJob(repo="o/r", pr_number=1, kind="update", head_sha=head, base_sha=base_sha,
                source="webhook")  # fmt: skip
    with pytest.raises(workdir.WorkdirError):
        update.process_update(job, deps)
    assert "환경 문제로 대기 중" in _comment(fix_env)
    assert "파이프라인 오류" not in _comment(fix_env)
    fix_env["monkeypatch"].setattr(retry, "attempt", lambda: (2, True))
    assert update.process_update(job, deps) == "passed"
    assert [p["branch"] for p in fix_env["pushes"]] == [BRANCH]


def test_model_fix_state_survives_checkpoint() -> None:
    fix = model_fix_rules.ModelFix(
        status="ready", targets=("v1",),
        explanation=model_fix_rules.Explanation(
            "s", (model_fix_rules.Change("a.py", 2, "R", "w"),)
        ),
        verification=model_fix_rules.Verification(9, 1, 1, False, 1, 2), attempt_id=3,
    )  # fmt: skip
    serde = JsonPlusSerializer(allowed_msgpack_modules=list(graph._STATE_TYPES))
    restored = serde.loads_typed(serde.dumps_typed({"f": fix}))["f"]
    assert isinstance(restored.explanation.changes[0], model_fix_rules.Change)
    assert isinstance(restored.verification, model_fix_rules.Verification)
