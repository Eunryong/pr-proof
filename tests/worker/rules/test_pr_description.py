"""PR 설명 확인의 실행 코드 (docs/DESIGN.md 4.5 끝): 모델을 부르지 않는 길. 설명이 비었을 때,
PR 사실을 읽지 못했을 때, 저장한 답을 다시 쓸 때와 쓰지 않을 때, 호출 상한, 파이프라인 수정안의
커밋. 변경이 커서 묶음으로 나눌 때는 호출을 나누고 모으는 배선만 본다(정해 둔 답을 돌려준다).
모델의 판단은 test_pr_description_real_cli.py가 실제 모델로 본다."""

import re
import subprocess
from pathlib import Path
from typing import Final

import pytest

from app.worker.config.repo_config_pure import CallSettings
from app.worker.git import repo
from app.worker.model.claude import workdir
from app.worker.model.claude.claude_backend import ClaudeCliBackend
from app.worker.model.claude.claude_cli import AIErrorKind, StructuredResult
from app.worker.rules import pr_description
from app.worker.rules.checkers import STALE
from app.worker.rules.diff_batches import saved_digests
from app.worker.rules.diff_batches_pure import split
from app.worker.rules.pr_check_pure import Commit, PrFacts
from app.worker.rules.pr_description_batch_pure import (
    BATCH_SCHEMA,
    BATCH_SYSTEM,
    BATCH_VERSION,
    MISSING_SYSTEM,
)
from app.worker.rules.pr_description_pure import (
    Mismatch,
    described_from,
    description_key,
    items_to_json,
    parse_diff,
    pipeline_text,
)
from app.worker.store.budget import CallBudget
from app.worker.store.state import Store

from ...redis_stand_ins import MemoryKeys

MODEL: Final = "sonnet"
BASE: Final = 'NAME = "demo"\nTIMEOUT = 30\n'
HEAD: Final = 'NAME = "demo"\nTIMEOUT = 5\n'


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip


@pytest.fixture
def wt(tmp_path: Path) -> Path:
    path = tmp_path / "wt"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    repo.record_git_dir(path)
    (path / "config.py").write_text(BASE)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "base")
    (path / "config.py").write_text(HEAD)
    _git(path, "commit", "-qam", "타임아웃을 5초로 줄인다")
    return path


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "pipeline.db", MemoryKeys())


def _never(*args, **kwargs):
    pytest.fail("이 경우에는 모델을 부르지 않아야 한다")


def _reading(ask):
    """claude 구현의 읽기 폴더에서 claude 대신 ask를 부른다."""
    return ClaudeCliBackend(run_structured=ask).reading


def _tools(store: Store, **limits: int) -> pr_description.Tools:
    budget = CallBudget(store, "o/r", 1, "a" * 40, CallSettings(**limits))
    return pr_description.Tools(store=store, budget=budget, read_code=_reading(_never), model=MODEL)


def _facts(title: str, body: str = "", messages: tuple[str, ...] = ()) -> PrFacts:
    commits = tuple(Commit(f"{n:040x}", m) for n, m in enumerate(messages, 1))
    return PrFacts(title=title, body=body, branch="b", commits=commits, paths=("config.py",))


def _diff(wt: Path) -> str:
    return _git(wt, "diff", "HEAD~1", "HEAD")


FACTS: Final = _facts("파서에서 빈 줄을 건너뛴다", "빈 줄이 있으면 멈추던 문제를 고친다.")
UNRELATED: Final = Mismatch("unrelated", "config.py에서 타임아웃을 30초에서 5초로 줄였습니다.",
                            file="config.py", side="new", line=2, code="TIMEOUT = 5")  # fmt: skip


def _save(store: Store, wt: Path, reads: dict[str, str]) -> None:
    key = description_key(described_from(FACTS), _diff(wt), pipeline_text(()), MODEL)
    store.save_result(key, stage="pr_description", model=MODEL, prompt_version="v",
                      result_json=items_to_json([UNRELATED], reads))  # fmt: skip


def test_unread_facts_empty_description_and_no_diff_make_no_call(wt: Path, store: Store) -> None:
    tools = _tools(store)
    out = pr_description.check("PR 사실을 읽지 못함: 404", _diff(wt), wt, tools)
    assert (out.status, out.gap, out.detail) == (
        "unchecked",
        "no_facts",
        "PR 사실을 읽지 못함: 404",
    )
    assert pr_description.check(_facts("Update", "", ("wip",)), _diff(wt), wt, tools).status == (
        "empty"
    )
    out = pr_description.check(FACTS, "", wt, tools)
    assert (out.status, out.gap) == ("unchecked", "no_diff")
    assert store.model_calls("o/r", 1) == []


def test_saved_answer_is_used_while_what_the_model_read_is_unchanged(
    wt: Path, store: Store
) -> None:
    digest = workdir.path_digest(wt, "config.py", repo.list_files(wt))
    _save(store, wt, {"config.py": digest})
    out = pr_description.check(FACTS, _diff(wt), wt, _tools(store))
    assert out.status == "checked" and out.items == (UNRELATED,)
    assert (out.usage.calls, out.usage.cached, out.files, out.shown) == (0, 1, 1, 1)
    (call,) = store.model_calls("o/r", 1)
    assert call.stage == "pr_description" and call.cache_hit


def test_saved_answer_is_not_used_once_a_read_file_changed(wt: Path, store: Store) -> None:
    """읽은 파일이 바뀌면 다시 묻는다. 여기서는 PR 상한이 0이라 묻지 못하고 "견주지 못함"이다."""
    _save(store, wt, {"config.py": "옛 해시"})
    out = pr_description.check(FACTS, _diff(wt), wt, _tools(store, pr_base=0))
    assert (out.status, out.gap) == ("unchecked", "cap") and out.items == ()
    assert out.usage.calls == 0


def test_next_commit_is_asked_with_the_earlier_result_and_what_changed(
    wt: Path, store: Store
) -> None:
    """저장한 답을 쓴 커밋의 결과를 그 PR의 바로 앞 결과로 두고, 다음 커밋은 그 결과와 PR 글이
    바뀌었는지, 그 커밋 뒤 바뀐 파일(git)을 같이 주고 묻는다. 호출은 실패로 끝난다."""
    first = _git(wt, "rev-parse", "HEAD").strip()
    _save(store, wt, {"config.py": workdir.path_digest(wt, "config.py", repo.list_files(wt))})
    budget = CallBudget(store, "o/r", 1, first, CallSettings())
    tools = pr_description.Tools(
        store=store, budget=budget, read_code=_reading(_never), model=MODEL
    )
    assert pr_description.check(FACTS, _diff(wt), wt, tools).usage.cached == 1
    (wt / "notes.txt").write_text("메모\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "메모")
    second = _git(wt, "rev-parse", "HEAD").strip()
    asked: list[str] = []

    def ask(prompt: str, **kw) -> StructuredResult:
        asked.append(prompt)
        return StructuredResult(ok=False, error=AIErrorKind.TIMEOUT, data=None, model_id="",
                                input_tokens=None, output_tokens=None, cost_usd=None,
                                permission_denials=0)  # fmt: skip

    budget = CallBudget(store, "o/r", 1, second, CallSettings())
    tools = pr_description.Tools(store=store, budget=budget, read_code=_reading(ask), model=MODEL)
    out = pr_description.check(FACTS, _git(wt, "diff", "HEAD~2", "HEAD"), wt, tools)
    (prompt,) = asked
    assert f"같은 PR의 앞 커밋 {first[:12]}에서 견준 결과다." in prompt
    assert f"1. unrelated (config.py:2, new: TIMEOUT = 5) — {UNRELATED.summary}" in prompt
    assert "PR 글(제목, 본문, 커밋 메시지): 그대로" in prompt
    assert "그 뒤 지금 커밋까지 바뀐 파일 1개:\nnotes.txt" in prompt
    assert (out.usage.earlier, out.usage.earlier_items) == (1, 1)
    other = CallBudget(store, "o/r", 2, second, CallSettings())  # 다른 PR은 그 결과를 받지 않는다
    pr_description.check(
        FACTS,
        _git(wt, "diff", "HEAD~2", "HEAD"),
        wt,
        pr_description.Tools(store=store, budget=other, read_code=_reading(ask), model=MODEL),
    )
    assert "<<<EARLIER" not in asked[-1]


def test_new_commit_stops_before_asking(wt: Path, store: Store) -> None:
    out = pr_description.check(FACTS, _diff(wt), wt, _tools(store), is_stale=lambda: True)
    assert (out.status, out.gap, out.detail) == ("unchecked", "cap", STALE)


def test_pipeline_commit_changes_are_read_for_the_model(wt: Path) -> None:
    head = _git(wt, "rev-parse", "HEAD").strip()
    facts = PrFacts(
        title="t", body="", branch="b", paths=(),
        commits=(Commit(head, "[pipeline] 수정안", pipeline=True), Commit("f" * 40, "사람"),
                 Commit("e" * 40, "머지", merge=True, pipeline=True),
                 Commit("d" * 40, "없는 커밋", pipeline=True)),
    )  # fmt: skip
    (patch,) = pr_description.pipeline_diffs(wt, facts)
    assert "+TIMEOUT = 5" in patch and "-TIMEOUT = 30" in patch


# --- 변경이 커서 묶음으로 나눌 때 ---

MISSING: Final = Mismatch("missing", "빈 줄을 건너뛰는 변경을 찾지 못했습니다.",
                          claim="빈 줄이 있으면 멈추던 문제를 고친다.")  # fmt: skip


@pytest.fixture
def big(wt: Path) -> Path:
    """호출 하나에 들어가지 않는 변경: config.py의 한 줄과 7만 자쯤의 새 파일 셋. 묶음은
    (config.py, gen0.py), (gen1.py), (gen2.py)다."""
    for n in range(3):
        (wt / f"gen{n}.py").write_text("".join(f"V{n}_{i} = '{'x' * 50}'\n" for i in range(900)))
    _git(wt, "add", "-A")
    _git(wt, "commit", "--amend", "-qm", "타임아웃을 5초로 줄인다")
    return wt


def _result(data: dict | None) -> StructuredResult:
    return StructuredResult(
        ok=data is not None, error=None if data is not None else AIErrorKind.TIMEOUT, data=data,
        model_id="claude-sonnet", input_tokens=100, output_tokens=10, cost_usd=None,
        permission_denials=0,
    )  # fmt: skip


def _item(m: Mismatch) -> dict:
    return {"kind": m.kind, "claim": m.claim, "file": m.file, "side": m.side, "line": m.line,
            "code": m.code, "summary": m.summary, "same_as": 0}  # fmt: skip


class Scripted:
    """묶음의 호출에는 그 묶음의 요약과(첫 묶음은 UNRELATED도), 마지막 호출에는 MISSING을 답한다.
    failing의 묶음은 실패한다. 호출마다 답할 종류가 아닌 것도 하나씩 섞어 답한다."""

    def __init__(self, failing: tuple[int, ...] = ()) -> None:
        self.failing = failing
        self.batches: dict[int, str] = {}
        self.final: list[str] = []
        self.schemas: list[object] = []

    def __call__(self, prompt: str, **kw) -> StructuredResult:
        if kw["system_prompt"] == MISSING_SYSTEM:
            self.final.append(prompt)
            return _result({"mismatches": [_item(MISSING), _item(UNRELATED)]})
        assert kw["system_prompt"] == BATCH_SYSTEM
        self.schemas.append(kw["schema"])
        n = int(re.search(r"중 (\d+)번째", prompt).group(1))
        self.batches[n] = prompt
        if n in self.failing:
            return _result(None)
        found = [_item(UNRELATED), _item(MISSING)] if n == 1 else []
        return _result({"mismatches": found, "changes": [f"묶음 {n}의 요약"]})


def _big_tools(store: Store, ask, **limits: int) -> pr_description.Tools:
    budget = CallBudget(store, "o/r", 1, "a" * 40, CallSettings(**limits))
    return pr_description.Tools(store=store, budget=budget, read_code=_reading(ask), model=MODEL)


def _big_key(wt: Path) -> str:
    return description_key(described_from(FACTS), _diff(wt), pipeline_text(()), MODEL,
                           BATCH_VERSION)  # fmt: skip


def test_large_change_is_asked_in_batches_and_the_answers_are_merged(
    big: Path, store: Store
) -> None:
    ask = Scripted()
    out = pr_description.check(FACTS, _diff(big), big, _big_tools(store, ask))
    assert out.status == "checked" and out.items == (UNRELATED, MISSING)
    assert (out.batches, out.files, out.shown, out.left) == (3, 4, 4, ())
    assert (out.usage.calls, out.usage.input_tokens, out.usage.models) == (
        4, 400, ("claude-sonnet",),
    )  # fmt: skip
    assert sorted(ask.batches) == [1, 2, 3] and ask.schemas == [BATCH_SCHEMA] * 3
    assert "+ TIMEOUT = 5" in ask.batches[1] and "V0_0" in ask.batches[1]
    assert "V1_0" in ask.batches[2] and "V0_0" not in ask.batches[2]
    assert (
        "<<<OTHER_FILES" in ask.batches[2] and "\nconfig.py\ngen0.py\ngen2.py\n" in (ask.batches[2])
    )
    assert "<<<PR_TITLE" in ask.batches[3] and FACTS.title in ask.batches[3]
    (final,) = ask.final
    assert "<<<DIFF" not in final and "<<<NOT_SEEN" not in final
    assert "묶음 1 (파일: config.py, gen0.py)\n- 묶음 1의 요약\n묶음 2 (파일: gen1.py)" in final
    calls = store.model_calls("o/r", 1)
    assert [(c.stage, c.ok) for c in calls] == [("pr_description", True)] * 4
    assert all(saved_digests(store, split(parse_diff(_diff(big))), MODEL))
    again = pr_description.check(FACTS, _diff(big), big, _tools(store))
    assert again.items == out.items and (again.usage.calls, again.usage.cached) == (0, 1)
    assert (again.batches, again.shown) == (3, 4)


def test_batches_the_call_cap_leaves_out_are_named_and_the_result_is_not_saved(
    big: Path, store: Store
) -> None:
    ask = Scripted()
    out = pr_description.check(FACTS, _diff(big), big, _big_tools(store, ask, pr_base=3))
    assert out.status == "checked" and out.items == (UNRELATED, MISSING)
    assert sorted(ask.batches) == [1, 2] and out.usage.calls == 3
    assert (out.left, out.shown, out.files) == (("gen2.py",), 3, 4)
    assert "<<<NOT_SEEN" in ask.final[0] and "\ngen2.py\n<<<END NOT_SEEN" in ask.final[0]
    assert store.cached_result(_big_key(big)) is None


def test_failed_batch_is_named_and_the_rest_is_still_compared(big: Path, store: Store) -> None:
    ask = Scripted(failing=(2,))
    out = pr_description.check(FACTS, _diff(big), big, _big_tools(store, ask))
    assert out.status == "checked" and out.items == (UNRELATED, MISSING)
    assert (out.left, out.shown) == (("gen1.py",), 3) and out.usage.calls == 4
    assert "묶음 2" not in ask.final[0] and "묶음 3 (파일: gen2.py)" in ask.final[0]
    assert [c.ok for c in store.model_calls("o/r", 1)] == [True, False, True, True]
    assert store.cached_result(_big_key(big)) is None


def test_large_change_is_not_compared_when_no_batch_can_be_asked(big: Path, store: Store) -> None:
    out = pr_description.check(FACTS, _diff(big), big, _big_tools(store, _never, pr_base=1))
    assert (out.status, out.gap, out.batches) == ("unchecked", "cap", 3)
    assert "PR 상한" in out.detail
    assert [c.ok for c in store.model_calls("o/r", 1)] == [False]
    failed = Scripted(failing=(1, 2, 3))
    out = pr_description.check(FACTS, _diff(big), big, _big_tools(store, failed, pr_base=10))
    assert (out.status, out.gap) == ("unchecked", "model_failed") and failed.final == []
