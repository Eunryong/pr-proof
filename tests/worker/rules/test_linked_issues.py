"""이슈 확인의 실행 코드 (docs/DESIGN.md 4.5 끝 "연결된 이슈 확인"): 모델을 부르지 않는 길. 이슈를
읽지 못했을 때, 연결된 이슈가 없을 때, 다른 저장소의 이슈만 있을 때, 저장한 답을 다시 쓸 때와 쓰지
않을 때, 호출 상한. 변경이 커서 묶음의 요약으로 견줄 때는 호출을 나누고 모으는 배선만 본다(정해 둔
답을 돌려준다). 모델의 판단은 test_linked_issues_real_cli.py가 실제 모델로 본다."""

import subprocess
from pathlib import Path
from typing import Final

import pytest

from app.worker.config.repo_config_pure import CallSettings
from app.worker.git import repo
from app.worker.git.git_pure import Issue, Linked
from app.worker.git.github import GitHub
from app.worker.model.claude import workdir
from app.worker.model.claude.claude_backend import ClaudeCliBackend
from app.worker.model.claude.claude_cli import StructuredResult
from app.worker.rules import linked_issues
from app.worker.rules.checkers import STALE
from app.worker.rules.diff_batches import save_digest
from app.worker.rules.diff_batches_pure import DIGEST_SYSTEM, split
from app.worker.rules.linked_issues_pure import (
    DIGEST_PROMPT_VERSION,
    DIGEST_SYSTEM_PROMPT,
    IssueGap,
    earlier_from_json,
    earlier_key,
    issues_key,
    items_to_json,
    select,
)
from app.worker.rules.pr_description_pure import parse_diff
from app.worker.store.budget import CallBudget
from app.worker.store.state import Store

from ...redis_stand_ins import MemoryKeys
from ..git.test_github import FakeGh

MODEL: Final = "sonnet"
HEAD_SHA: Final = "a" * 40
ISSUE: Final = Issue(
    7, "o/r", "타임아웃을 5초로 줄여 주세요", "지금 30초라 너무 깁니다.\n- 재시도도 3회로"
)
LINKED: Final = Linked((ISSUE,), 1, 1)
GAP: Final = IssueGap(7, "missing", "재시도 횟수는 그대로입니다.", "- 재시도도 3회로")


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
    (path / "config.py").write_text("TIMEOUT = 30\nRETRIES = 2\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "base")
    (path / "config.py").write_text("TIMEOUT = 5\nRETRIES = 2\n")
    _git(path, "commit", "-qam", "타임아웃을 줄인다")
    return path


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "pipeline.db", MemoryKeys())


def _never(*args, **kwargs):
    pytest.fail("이 경우에는 모델을 부르지 않아야 한다")


def _reading(ask):
    """claude 구현의 읽기 폴더에서 claude 대신 ask를 부른다."""
    return ClaudeCliBackend(run_structured=ask).reading


def _tools(store: Store, **limits: int) -> linked_issues.Tools:
    budget = CallBudget(store, "o/r", 1, HEAD_SHA, CallSettings(**limits))
    return linked_issues.Tools(store=store, budget=budget, read_code=_reading(_never), model=MODEL)


def _diff(wt: Path) -> str:
    return _git(wt, "diff", "HEAD~1", "HEAD")


def _save(store: Store, wt: Path, reads: dict[str, str]) -> None:
    key = issues_key(select(LINKED, "o/r"), _diff(wt), MODEL)
    store.save_result(key, stage="linked_issues", model=MODEL, prompt_version="v",
                      result_json=items_to_json([GAP], reads))  # fmt: skip


def test_reading_failure_becomes_a_reason() -> None:
    gh = FakeGh()
    gh.linked_error = "gh api graphql 실패: HTTP 403: Resource not accessible"
    out = linked_issues.read(GitHub(gh), "o/r", 1)
    assert out == "연결된 이슈를 읽지 못함: gh api graphql 실패: HTTP 403: Resource not accessible"
    assert linked_issues.read(GitHub(FakeGh()), "o/r", 1) == Linked((), 0, 0, 0)


def test_no_call_without_an_issue_to_compare(wt: Path, store: Store) -> None:
    tools = _tools(store)
    out = linked_issues.check("연결된 이슈를 읽지 못함: 403", "o/r", _diff(wt), wt, tools)
    assert (out.status, out.gap, out.detail) == ("unchecked", "unreadable",
                                                 "연결된 이슈를 읽지 못함: 403")  # fmt: skip
    assert linked_issues.check(Linked(), "o/r", _diff(wt), wt, tools).status == "none"
    other = linked_issues.check(Linked((Issue(3, "x/y", "t", "b"),), 1, 1), "o/r", _diff(wt), wt,
                                tools)  # fmt: skip
    assert (other.status, other.gap, other.other_repo) == ("unchecked", "other_repo", ("x/y#3",))
    hidden = linked_issues.check(Linked((), 1, 1, unreadable=1), "o/r", _diff(wt), wt, tools)
    assert (hidden.status, hidden.gap, hidden.unreadable) == ("unchecked", "unreadable", 1)
    no_diff = linked_issues.check(LINKED, "o/r", "", wt, tools)
    assert (no_diff.status, no_diff.gap, no_diff.issues) == ("unchecked", "no_diff", (7,))
    assert store.model_calls("o/r", 1) == []


def test_saved_answer_is_used_while_what_the_model_read_is_unchanged(
    wt: Path, store: Store
) -> None:
    """저장한 답을 쓰고, 이 커밋의 결과를 이 PR의 바로 앞 결과로 둔다(다음 커밋이 받는다)."""
    _save(store, wt, {"config.py": workdir.path_digest(wt, "config.py", repo.list_files(wt))})
    out = linked_issues.check(LINKED, "o/r", _diff(wt), wt, _tools(store))
    assert out.status == "checked" and out.items == (GAP,) and out.issues == (7,)
    assert (out.usage.calls, out.usage.cached, out.files, out.shown) == (0, 1, 1, 1)
    (call,) = store.model_calls("o/r", 1)
    assert call.stage == "linked_issues" and call.cache_hit
    pointer = store.cached_result(earlier_key("o/r#1", MODEL))
    assert pointer is not None
    earlier = earlier_from_json(pointer.result_json, select(LINKED, "o/r"))
    assert (earlier.items, earlier.sha, earlier.text_changed) == ((GAP,), HEAD_SHA, False)


def test_saved_answer_is_not_used_once_a_read_file_changed(wt: Path, store: Store) -> None:
    """읽은 파일이 바뀌면 다시 묻는다. 여기서는 PR 상한이 0이라 묻지 못하고 "견주지 못함"이다."""
    _save(store, wt, {"config.py": "옛 해시"})
    out = linked_issues.check(LINKED, "o/r", _diff(wt), wt, _tools(store, pr_base=0))
    assert (out.status, out.gap, out.issues) == ("unchecked", "cap", (7,)) and out.items == ()
    assert out.usage.calls == 0


def test_saved_answer_is_not_used_once_the_issue_text_changed(wt: Path, store: Store) -> None:
    _save(store, wt, {})
    edited = Linked((Issue(7, "o/r", ISSUE.title, ISSUE.body + " 그리고 로그도"),), 1, 1)
    out = linked_issues.check(edited, "o/r", _diff(wt), wt, _tools(store, pr_base=0))
    assert (out.status, out.gap) == ("unchecked", "cap")


def test_new_commit_stops_before_asking(wt: Path, store: Store) -> None:
    out = linked_issues.check(LINKED, "o/r", _diff(wt), wt, _tools(store), is_stale=lambda: True)
    assert (out.status, out.gap, out.detail) == ("unchecked", "cap", STALE)


# --- 변경이 커서 묶음의 요약으로 견줄 때 ---


@pytest.fixture
def big(wt: Path) -> Path:
    """호출 하나에 들어가지 않는 변경. 묶음은 (config.py, gen0.py), (gen1.py), (gen2.py)다."""
    for n in range(3):
        (wt / f"gen{n}.py").write_text("".join(f"V{n}_{i} = '{'x' * 50}'\n" for i in range(900)))
    _git(wt, "add", "-A")
    _git(wt, "commit", "--amend", "-qm", "타임아웃을 줄인다")
    return wt


class Scripted:
    """요약을 받는 호출에는 그 묶음의 요약을, 견주는 호출에는 GAP을 답한다."""

    def __init__(self) -> None:
        self.digests: list[str] = []
        self.final: list[str] = []

    def __call__(self, prompt: str, **kw) -> StructuredResult:
        if kw["system_prompt"] == DIGEST_SYSTEM:
            self.digests.append(prompt)
            data: dict = {"changes": ["요약 " + prompt.split("나눈 것 중 ")[1][:1]]}
        else:
            assert kw["system_prompt"] == DIGEST_SYSTEM_PROMPT
            self.final.append(prompt)
            data = {"gaps": [{"issue": 7, "kind": "missing", "quote": GAP.quote,
                              "summary": GAP.summary, "same_as": 0}]}  # fmt: skip
        return StructuredResult(ok=True, error=None, data=data, model_id="claude-sonnet",
                                input_tokens=100, output_tokens=10, cost_usd=None,
                                permission_denials=0)  # fmt: skip


def _big_tools(store: Store, ask, **limits: int) -> linked_issues.Tools:
    budget = CallBudget(store, "o/r", 1, HEAD_SHA, CallSettings(**limits))
    return linked_issues.Tools(store=store, budget=budget, read_code=_reading(ask), model=MODEL)


def test_large_change_is_compared_from_the_batch_digests(big: Path, store: Store) -> None:
    ask = Scripted()
    out = linked_issues.check(LINKED, "o/r", _diff(big), big, _big_tools(store, ask))
    assert out.status == "checked" and out.items == (GAP,)
    assert (out.batches, out.files, out.shown, out.left, out.usage.calls) == (3, 4, 4, (), 4)
    assert len(ask.digests) == 3 and "<<<ISSUE_TITLE" not in ask.digests[0]
    (final,) = ask.final
    assert "<<<DIFF" not in final and "<<<ISSUE_TITLE number=7" in final
    assert "묶음 1 (파일: config.py, gen0.py)\n- 요약 1\n묶음 2 (파일: gen1.py)\n- 요약 2" in final
    key = issues_key(select(LINKED, "o/r"), _diff(big), MODEL, DIGEST_PROMPT_VERSION)
    assert store.cached_result(key) is not None
    again = linked_issues.check(LINKED, "o/r", _diff(big), big, _tools(store))
    assert again.items == (GAP,) and (again.usage.calls, again.usage.cached) == (0, 1)


def test_saved_digests_are_reused_and_only_missing_ones_are_asked(big: Path, store: Store) -> None:
    """PR 설명 확인이 묶음을 읽으며 저장한 요약이 있으면 그 묶음은 다시 요약하지 않는다."""
    parts = split(parse_diff(_diff(big)))
    save_digest(store, parts.batches[0], ("저장한 요약",), MODEL)
    save_digest(store, parts.batches[2], ("저장한 요약 셋",), MODEL)
    ask = Scripted()
    out = linked_issues.check(LINKED, "o/r", _diff(big), big, _big_tools(store, ask))
    assert out.usage.calls == 2 and len(ask.digests) == 1 and "V1_0" in ask.digests[0]
    assert "- 저장한 요약\n" in ask.final[0] and "- 요약 2\n" in ask.final[0]
    assert "- 저장한 요약 셋\n" in ask.final[0]


def test_batches_without_a_digest_are_named_when_the_cap_is_short(big: Path, store: Store) -> None:
    ask = Scripted()
    out = linked_issues.check(LINKED, "o/r", _diff(big), big, _big_tools(store, ask, pr_base=2))
    assert out.status == "checked" and out.usage.calls == 2 and len(ask.digests) == 1
    assert (out.left, out.shown) == (("gen1.py", "gen2.py"), 2)
    assert "<<<NOT_SEEN" in ask.final[0] and "묶음 2" not in ask.final[0]
    key = issues_key(select(LINKED, "o/r"), _diff(big), MODEL, DIGEST_PROMPT_VERSION)
    assert store.cached_result(key) is None
    none = linked_issues.check(LINKED, "o/r", _diff(big), big, _big_tools(store, _never, pr_base=2))
    assert (none.status, none.gap) == ("unchecked", "cap")
