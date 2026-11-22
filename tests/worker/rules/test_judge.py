"""app/worker/rules/judge.py: 파일 단위로 묶어 묻기, (규칙, hunk) 쌍마다 저장하고 다시 쓰기(4-2), 줄
인용 확인(4-3), 호출 상한과 대상 위반 수(4-5), 모델 호출 실패."""

import re
import subprocess
from pathlib import Path
from typing import Final

import fakeredis
import pytest

from app.worker import repo
from app.worker.ai import workdir
from app.worker.ai.claude_cli import AIErrorKind, StructuredResult
from app.worker.budget import CallBudget
from app.worker.diff_pure import added_lines
from app.worker.repo_config_pure import CallSettings
from app.worker.rule_pure import CheckMethod, Rule, RuleSource
from app.worker.rules.detect import source_lines
from app.worker.rules.judge import Judged, judge
from app.worker.rules.judge_pure import (
    ANSWER_SCHEMA,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    flags_to_json,
    hunks_for,
    judge_key,
)
from app.worker.state import Store

HEAD: Final = "a" * 40
NAMES: Final = Rule(
    id="R-readme-names", text="함수 이름은 하는 일을 동사로 시작한다.",
    source=RuleSource("README.md", "코드 규칙 > 이름", "b"), origin="document", scope=(),
    check=CheckMethod("checker"), fix="model",
)  # fmt: skip
MONEY: Final = Rule(
    id="R-readme-money", text="금액은 원 단위 정수로 다룬다.",
    source=RuleSource("README.md", "코드 규칙 > 금액", "b"), origin="document",
    scope=("orders/**",), check=CheckMethod("checker"), fix="model",
)  # fmt: skip
PRICING: Final = "def line_amount(item):\n    return item.price * item.quantity\n"
HANDLER: Final = "def body(order):\n    return {}\n"


def _diff(file: str, text: str) -> str:
    lines = text.splitlines()
    return (f"diff --git a/{file} b/{file}\nnew file mode 100644\n--- /dev/null\n+++ b/{file}\n"
            f"@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{line}\n" for line in lines))  # fmt: skip


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    root = tmp_path / "wt"
    (root / "orders").mkdir(parents=True)
    (root / "orders/pricing.py").write_text(PRICING)
    (root / "api.py").write_text(HANDLER)
    (root / "README.md").write_text("# x\n")
    # 판단은 worktree의 파일을 모델 폴더로 복사해 읽게 한다 (git ls-files로 고른다)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    repo.record_git_dir(root)
    return root


DIFF: Final = (
    _diff("orders/pricing.py", PRICING) + _diff("api.py", HANDLER) + _diff("README.md", "# x\n")
)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "pipeline.db", fakeredis.FakeStrictRedis())


class FakeModel:
    """answers: 파일 → 그 파일을 물을 때의 답 목록. 부른 기록을 남긴다."""

    def __init__(self, answers: dict[str, list[dict]] | None = None,
                 result: StructuredResult | None = None) -> None:  # fmt: skip
        self.answers = answers or {}
        self.result = result
        self.calls: list[dict] = []

    def __call__(self, prompt: str, **kw) -> StructuredResult:
        files = set(re.findall(r"^파일: (.+)$", prompt, re.M))
        self.calls.append({"prompt": prompt, "files": files, **kw})
        if self.result is not None:
            return self.result
        found = [a for f in files for a in self.answers.get(f, [])]
        return StructuredResult(
            ok=True, error=None, data={"violations": found}, model_id="claude-sonnet-5",
            input_tokens=300, output_tokens=40, cost_usd=0.01, permission_denials=0,
        )  # fmt: skip


VERB: Final = {"rule_id": NAMES.id, "file": "orders/pricing.py", "line": 1,
               "quote": "def line_amount(item):", "why": "동사로 시작하지 않음"}  # fmt: skip


def _run(store: Store, model: FakeModel, worktree: Path, *, diff: str = DIFF,
         rules: tuple[Rule, ...] = (NAMES, MONEY), calls: CallSettings | None = None,
         known: int = 0) -> Judged:  # fmt: skip
    budget = CallBudget(store, "o/r", 1, HEAD, calls or CallSettings())
    return judge(rules, worktree, diff, store=store, budget=budget, model="sonnet", ask=model,
                 known=known)  # fmt: skip


def test_one_tool_less_call_per_file_with_only_the_rules_in_scope(store, worktree) -> None:
    model = FakeModel({"orders/pricing.py": [VERB]})
    result = _run(store, model, worktree)
    assert [sorted(c["files"]) for c in model.calls] == [["api.py"], ["orders/pricing.py"]]
    api, pricing = model.calls[0]["prompt"], model.calls[1]["prompt"]
    assert MONEY.id not in api and NAMES.id in api  # 범위 밖 규칙은 묻지 않는다
    assert MONEY.id in pricing and NAMES.id in pricing
    assert all(c["system_prompt"] == SYSTEM_PROMPT and c["schema"] is ANSWER_SCHEMA
               for c in model.calls)  # fmt: skip
    (v,) = result.violations
    assert (v.rule_id, v.file, v.line, v.found_by) == (NAMES.id, "orders/pricing.py", 1, "model")
    assert (v.line_text, v.message) == ("def line_amount(item):", "동사로 시작하지 않음")
    assert result.usage.calls == 2 and result.usage.cached == 0 and result.gaps == {}
    assert [c.stage for c in store.model_calls("o/r", 1)] == ["judge", "judge"]


def test_second_run_on_the_same_hunks_makes_no_call_and_gives_the_same_answer(
    store, worktree
) -> None:
    """4-2: (규칙, hunk) 쌍마다 저장한 답을 쓴다. 위반이 없던 쌍도 저장돼 다시 묻지 않는다."""
    first = _run(store, FakeModel({"orders/pricing.py": [VERB]}), worktree)
    again = FakeModel()
    second = _run(store, again, worktree)
    assert again.calls == [] and second.violations == first.violations
    assert second.usage.calls == 0 and second.usage.cached == 3


def _appended(pad: int) -> str:
    """앞에 `# pad` 줄이 pad개 있는 파일 끝에 PRICING을 더한 diff."""
    lines = PRICING.splitlines()
    return (f"diff --git a/orders/pricing.py b/orders/pricing.py\n--- a/orders/pricing.py\n"
            f"+++ b/orders/pricing.py\n@@ -{pad},0 +{pad + 1},{len(lines)} @@\n"
            + "".join(f"+{line}\n" for line in lines))  # fmt: skip


def test_hunk_moved_down_by_an_unrelated_change_is_not_asked_again(store, worktree) -> None:
    """위쪽(문맥 20줄 밖)에 줄이 더해져도 hunk 내용이 같으면 저장한 답을 그 줄의 새 번호로 쓴다."""
    path = worktree / "orders/pricing.py"
    path.write_text("# pad\n" * 25 + PRICING)
    moved_verb = {**VERB, "line": 26}
    _run(store, FakeModel({"orders/pricing.py": [moved_verb]}), worktree, diff=_appended(25),
         rules=(NAMES,))  # fmt: skip
    path.write_text("# pad\n" * 55 + PRICING)
    again = FakeModel()
    result = _run(store, again, worktree, diff=_appended(55), rules=(NAMES,))
    assert again.calls == [] and [v.line for v in result.violations] == [56]


def test_answer_pointing_at_a_line_outside_the_diff_is_dropped(store, worktree) -> None:
    """4-3: diff에 없는 줄, 인용이 다른 답은 버리고 남은 답만 위반이 된다."""
    bad = [{**VERB, "line": 7, "quote": "nothing here"},
           {**VERB, "line": 2, "quote": "return item.price"}]  # fmt: skip
    result = _run(store, FakeModel({"orders/pricing.py": bad}), worktree)
    assert result.violations == () and result.usage.dropped == 2


def test_failed_call_leaves_the_pairs_unjudged_and_unsaved(store, worktree) -> None:
    failed = StructuredResult(ok=False, error=AIErrorKind.TIMEOUT, data=None, model_id="",
                              input_tokens=None, output_tokens=None, cost_usd=None,
                              permission_denials=0, detail="600초")  # fmt: skip
    result = _run(store, FakeModel(result=failed), worktree)
    assert result.violations == ()
    assert {k: (g.kind, g.pairs) for k, g in result.gaps.items()} == {
        NAMES.id: ("model_failed", 2),
        MONEY.id: ("model_failed", 1),
    }
    retry = FakeModel({"orders/pricing.py": [VERB]})
    assert _run(store, retry, worktree).violations and len(retry.calls) == 2


def test_malformed_answer_counts_as_a_failed_call(store, worktree) -> None:
    broken = StructuredResult(ok=True, error=None, data={"answer": "없음"}, model_id="m",
                              input_tokens=1, output_tokens=1, cost_usd=0.0,
                              permission_denials=0)  # fmt: skip
    result = _run(store, FakeModel(result=broken), worktree)
    assert {g.kind for g in result.gaps.values()} == {"model_failed"}
    assert all(not c.ok for c in store.model_calls("o/r", 1))


def test_pr_cap_stops_judging_and_leaves_the_rest(store, worktree) -> None:
    """4-6: PR 상한에 걸리면 남은 쌍을 묻지 않고 규칙마다 판단하지 못한 수로 남긴다."""
    model = FakeModel({"orders/pricing.py": [VERB]})
    result = _run(store, model, worktree, calls=CallSettings(pr_base=1, per_violation=0))
    assert len(model.calls) == 1 and model.calls[0]["files"] == {"api.py"}
    assert {k: (g.kind, g.pairs) for k, g in result.gaps.items()} == {
        NAMES.id: ("cap", 1),
        MONEY.id: ("cap", 1),
    }
    assert "PR 상한" in result.gaps[NAMES.id].detail


def test_pr_cap_grows_with_violations_already_found(store, worktree) -> None:
    """4-5: 대상 위반 수만큼 PR 상한이 는다. 도구·검사기가 찾은 위반(known)도 센다."""
    calls = CallSettings(pr_base=1, per_violation=1)
    model = FakeModel()
    assert _run(store, model, worktree, calls=calls, known=1).gaps == {}
    assert len(model.calls) == 2


def test_daily_cap_stops_judging(store, worktree) -> None:
    result = _run(store, FakeModel(), worktree, calls=CallSettings(repo_daily=0))
    assert {g.kind for g in result.gaps.values()} == {"cap"}
    assert "저장소 하루 상한" in result.gaps[NAMES.id].detail


def test_unreadable_or_non_python_files_are_not_judged(store, worktree) -> None:
    (worktree / "api.py").unlink()
    (worktree / "api.py").symlink_to(worktree / "orders/pricing.py")
    model = FakeModel()
    _run(store, model, worktree)
    assert [c["files"] for c in model.calls] == [{"orders/pricing.py"}]


def test_no_rules_means_no_call(store, worktree) -> None:
    model = FakeModel()
    assert _run(store, model, worktree, rules=()).violations == () and model.calls == []


def test_undecided_pairs_are_kept_with_the_reason_and_reused(store: Store, worktree: Path) -> None:
    """모델이 판단하지 못했다고 답한 쌍은 까닭과 함께 저장되고, 다음 검사가 묻지 않고 같은 답을
    쓴다."""
    answer = StructuredResult(
        ok=True, error=None, model_id="claude-sonnet-5", input_tokens=1, output_tokens=1,
        cost_usd=0.0, permission_denials=0,
        data={"violations": [], "undecided": [{"rule_id": MONEY.id, "why": "데이터를 봐야 앎"}]},
    )  # fmt: skip
    first = _run(store, FakeModel(result=answer), worktree)
    again = FakeModel()
    second = _run(store, again, worktree)
    assert first.violations == second.violations == ()
    assert first.undecided[MONEY.id].why == second.undecided[MONEY.id].why == "데이터를 봐야 앎"
    assert first.undecided[MONEY.id].pairs == first.pairs[MONEY.id] == 1
    assert NAMES.id not in first.undecided and first.pairs[NAMES.id] == 2
    assert again.calls == []


def _save_answer(store: Store, worktree: Path, reads: dict[str, str]) -> None:
    """api.py의 hunk에 대한 NAMES의 답(위반 없음)을, 모델이 reads를 읽고 낸 것으로 저장해 둔다."""
    changed = added_lines(_diff_api())["api.py"]
    (hunk,) = hunks_for("api.py", source_lines(worktree, "api.py"), changed)
    store.save_result(judge_key(NAMES, hunk, "sonnet"), stage="judge", model="sonnet",
                      prompt_version=PROMPT_VERSION,
                      result_json=flags_to_json(hunk, [], reads))  # fmt: skip


def _diff_api() -> str:
    return _diff("api.py", HANDLER)


def test_stored_answer_is_reused_only_while_the_files_the_model_read_are_unchanged(
    store: Store, worktree: Path
) -> None:
    """답이 hunk 밖의 코드에도 달려 있으므로, 같은 hunk여도 모델이 읽은 파일이 바뀌면 다시
    묻는다."""
    files = repo.list_files(worktree)
    reads = {p: workdir.path_digest(worktree, p, files) for p in ("orders/pricing.py", "orders")}
    _save_answer(store, worktree, reads)
    asked: list[str] = []

    def ask(prompt: str, **kw) -> StructuredResult:  # 부르면 실패로 끝나는 호출
        asked.append(prompt)
        return StructuredResult(ok=False, error=AIErrorKind.TIMEOUT, data=None, model_id="",
                                input_tokens=None, output_tokens=None, cost_usd=None,
                                permission_denials=0)  # fmt: skip

    def run() -> Judged:
        budget = CallBudget(store, "o/r", 1, HEAD, CallSettings())
        return judge((NAMES,), worktree, _diff_api(), store=store, budget=budget, model="sonnet",
                     ask=ask)  # fmt: skip

    (worktree / "README.md").write_text("# 읽지 않은 파일만 바뀜\n")
    kept = run()
    assert asked == [] and kept.usage.cached == 1 and kept.gaps == {}
    (worktree / "orders/pricing.py").write_text(PRICING + "RATE = 2\n")
    changed = run()
    assert len(asked) == 1 and changed.usage.cached == 0
    assert changed.gaps[NAMES.id].kind == "model_failed"
