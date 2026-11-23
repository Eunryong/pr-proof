"""PR 설명 확인의 실행 코드 (docs/DESIGN.md 4.5 끝): 모델을 부르지 않는 길. 설명이 비었을 때,
PR 사실을 읽지 못했을 때, 저장한 답을 다시 쓸 때와 쓰지 않을 때, 호출 상한, 파이프라인 수정안의
커밋. 모델을 부르는 길은 test_pr_description_real_cli.py가 실제 모델로 본다."""

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
from app.worker.rules.pr_check_pure import Commit, PrFacts
from app.worker.rules.pr_description_pure import (
    Mismatch,
    described_from,
    description_key,
    items_to_json,
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
