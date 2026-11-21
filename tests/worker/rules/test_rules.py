"""worker/rules/rules.py: base의 문서로 규칙 묶음을 만든다. 로컬 git, SQLite, 가짜 모델로
확인한다."""

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import fakeredis
import pytest

from worker import repo
from worker.ai.claude_cli import AIErrorKind, StructuredResult
from worker.budget import CallBudget
from worker.call_budget_pure import Denied
from worker.repo_config_pure import CallSettings, RulesSettings
from worker.rule_reading_pure import ANSWER_SCHEMA, SYSTEM_PROMPT
from worker.rules.rules import (
    CapReached,
    ModelFailed,
    NoRuleDocuments,
    NoRules,
    RulesFound,
    load_rules,
)
from worker.state import Store

README: Final = (Path(__file__).parents[2] / "fixtures" / "rules" / "README.md").read_text()
PRINT_RULE: Final = (
    "`orders/` 아래 모듈은 `print`를 쓰지 않는다. 남길 것이 있으면 `logging`의 로거를 쓴다."
)
FROZEN_RULE: Final = "`orders/models`의 데이터 클래스는 `@dataclass(frozen=True)`로 선언한다."
NOW: Final = datetime(2026, 11, 18, 12, tzinfo=UTC)


def _rule(quote: str, section: str, file: str = "README.md") -> dict:
    return {"file": file, "section": section, "quote": quote, "scope": [], "examples": [],
            "check": "checker", "reason": "", "fix": "model"}  # fmt: skip


def _answer(*rules: dict, **fields: object) -> StructuredResult:
    values = {
        "ok": True, "error": None, "data": {"rules": list(rules)}, "model_id": "claude-haiku-5-5",
        "input_tokens": 3000, "output_tokens": 200, "cost_usd": 0.001, "permission_denials": 0,
    }  # fmt: skip
    values.update(fields)
    return StructuredResult(**values)


class FakeModel:
    def __init__(self, *answers: StructuredResult) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, prompt: str, **kwargs: object) -> StructuredResult:
        self.calls.append((prompt, kwargs))
        return self.answers.pop(0)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip


def _commit(path: Path, files: dict[str, str | bytes]) -> str:
    for name, content in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "--allow-empty", "-m", "c")
    return _git(path, "rev-parse", "HEAD")


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    path = tmp_path / "wt"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    repo.record_git_dir(path)
    return path


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "pipeline.db", fakeredis.FakeStrictRedis())


def _load(worktree: Path, base: str, store: Store, model: FakeModel, *, pr: int = 1,
          settings: RulesSettings | None = None, **calls: int):  # fmt: skip
    budget = CallBudget(store, "o/r", pr, "head", CallSettings(**calls), now=lambda: NOW)
    return load_rules(worktree, base, settings or RulesSettings(), model="haiku", store=store,
                      budget=budget, ask=model)  # fmt: skip


def test_reads_rules_from_base_and_drops_unverified_ones(worktree: Path, store: Store) -> None:
    base = _commit(worktree, {"README.md": README})
    _commit(worktree, {"README.md": README + "\n- 모든 함수에 docstring을 단다.\n"})  # PR head
    model = FakeModel(
        _answer(
            _rule(PRINT_RULE, "코드 규칙 > 출력"),
            _rule("모든 함수에 docstring을 단다.", "코드 규칙"),
        )
    )
    result = _load(worktree, base, store, model)

    assert isinstance(result, RulesFound) and not result.cached
    assert [r.text for r in result.rules] == [PRINT_RULE]
    assert result.rules[0].source.file == "README.md"
    assert result.rules[0].source.section == "코드 규칙 > 출력"
    assert [d.quote for d in result.ruleset.dropped] == ["모든 함수에 docstring을 단다."]
    assert result.ruleset.model == "claude-haiku-5-5"

    prompt, kwargs = model.calls[0]
    assert PRINT_RULE in prompt and "docstring" not in prompt  # head가 아니라 base의 문서
    assert kwargs["system_prompt"] == SYSTEM_PROMPT and kwargs["schema"] == ANSWER_SCHEMA
    assert kwargs["model"] == "haiku"
    [call] = store.model_calls("o/r", 1)
    assert (call.stage, call.counted, call.ok, call.cache_hit) == ("rules", 1, True, False)
    assert (call.model_id, call.input_tokens, call.ruleset_key) == (
        "claude-haiku-5-5", 3000, result.key,
    )  # fmt: skip


def test_second_check_uses_the_saved_ruleset(worktree: Path, store: Store) -> None:
    base = _commit(worktree, {"README.md": README})
    first = _load(worktree, base, store, FakeModel(_answer(_rule(PRINT_RULE, "출력"))))
    model = FakeModel()
    second = _load(worktree, base, store, model, pr=2)
    assert isinstance(second, RulesFound) and second.cached
    assert second.ruleset == first.ruleset
    assert model.calls == []
    [hit] = store.model_calls("o/r", 2)
    assert (hit.counted, hit.cache_hit) == (0, True)


def test_changed_rule_document_on_base_is_read_again(worktree: Path, store: Store) -> None:
    base = _commit(worktree, {"README.md": README})
    _load(worktree, base, store, FakeModel(_answer(_rule(PRINT_RULE, "출력"))))
    new_base = _commit(worktree, {"README.md": README.replace(PRINT_RULE, "")})
    model = FakeModel(_answer(_rule(FROZEN_RULE, "모델")))
    result = _load(worktree, new_base, store, model)
    assert len(model.calls) == 1
    assert [r.text for r in result.rules] == [FROZEN_RULE]


def test_disabled_rules_are_left_out_but_kept_in_the_saved_ruleset(
    worktree: Path, store: Store
) -> None:
    base = _commit(worktree, {"README.md": README})
    answer = _answer(_rule(PRINT_RULE, "출력"), _rule(FROZEN_RULE, "모델"))
    first = _load(worktree, base, store, FakeModel(answer))
    off = first.rules[0].id
    result = _load(worktree, base, store, FakeModel(), settings=RulesSettings(disabled=[off]))
    assert [r.text for r in result.rules] == [FROZEN_RULE]
    assert len(result.ruleset.rules) == 2
    everything_off = RulesSettings(disabled=[r.id for r in first.rules])
    result = _load(worktree, base, store, FakeModel(), settings=everything_off)
    assert isinstance(result, RulesFound) and result.rules == ()


def test_no_rule_documents_calls_no_model(worktree: Path, store: Store) -> None:
    base = _commit(worktree, {"app.py": "x = 1\n", "sub/README.md": README})
    model = FakeModel()
    assert _load(worktree, base, store, model) == NoRuleDocuments(unread=())
    assert _load(worktree, base, store, model, settings=RulesSettings(docs=[])) == NoRuleDocuments(
        ()
    )
    assert model.calls == [] and store.model_calls("o/r", 1) == []


def test_symlinked_readme_is_not_read(worktree: Path, store: Store) -> None:
    (worktree / "notes.md").write_text(README)
    os.symlink("notes.md", worktree / "README.md")
    base = _commit(worktree, {})
    model = FakeModel()
    result = _load(worktree, base, store, model)
    assert isinstance(result, NoRuleDocuments)
    assert [u.path for u in result.unread] == ["README.md"]
    assert model.calls == []


def test_non_utf8_document_is_skipped_and_others_are_read(worktree: Path, store: Store) -> None:
    base = _commit(
        worktree,
        {"README.md": "# 규칙\n- 금지\n".encode("cp949"), "CONTRIBUTING.md": README},
    )
    model = FakeModel(_answer(_rule(PRINT_RULE, "출력", file="CONTRIBUTING.md")))
    result = _load(worktree, base, store, model)
    assert isinstance(result, RulesFound)
    assert [u.path for u in result.ruleset.unread] == ["README.md"]
    assert result.ruleset.sources[0][0] == "CONTRIBUTING.md"
    assert '<<<DOCUMENT path="README.md"' not in model.calls[0][0]


def test_rules_docs_reads_nested_documents(worktree: Path, store: Store) -> None:
    base = _commit(worktree, {"README.md": "# x\n", "docs/rules.md": README})
    model = FakeModel(_answer(_rule(PRINT_RULE, "출력", file="docs/rules.md")))
    settings = RulesSettings(docs=["docs/rules.md"])
    result = _load(worktree, base, store, model, settings=settings)
    assert isinstance(result, RulesFound)
    assert result.rules[0].id.startswith("R-rules-")
    assert '<<<DOCUMENT path="README.md"' not in model.calls[0][0]


def test_no_verified_rules_is_saved_too(worktree: Path, store: Store) -> None:
    base = _commit(worktree, {"README.md": README})
    first = _load(worktree, base, store, FakeModel(_answer()))
    assert isinstance(first, NoRules) and not first.cached
    model = FakeModel()
    second = _load(worktree, base, store, model)
    assert isinstance(second, NoRules) and second.cached and model.calls == []


@pytest.mark.parametrize(
    "answer",
    [
        _answer(ok=False, error=AIErrorKind.TIMEOUT, data=None, detail="600초 안에 끝나지 않음"),
        _answer(ok=False, error=AIErrorKind.EXIT, data=None, model_id=""),
        _answer(data={"rules": [{"file": "README.md"}]}),
    ],
    ids=["timeout", "exit", "bad-shape"],
)
def test_model_failure_is_counted_and_retried_next_time(
    worktree: Path, store: Store, answer: StructuredResult
) -> None:
    base = _commit(worktree, {"README.md": README})
    result = _load(worktree, base, store, FakeModel(answer))
    assert isinstance(result, ModelFailed) and result.reason
    [call] = store.model_calls("o/r", 1)
    assert (call.counted, call.ok) == (1, False)
    retry = FakeModel(_answer(_rule(PRINT_RULE, "출력")))
    assert isinstance(_load(worktree, base, store, retry), RulesFound)
    assert len(retry.calls) == 1


def test_ruleset_cap_stops_reading(worktree: Path, store: Store) -> None:
    base = _commit(worktree, {"README.md": README})
    model = FakeModel()
    result = _load(worktree, base, store, model, ruleset_base=0)
    assert isinstance(result, CapReached) and result.denied == Denied("ruleset", 0, 0, 1)
    assert model.calls == [] and store.model_calls("o/r", 1) == []


def test_repo_daily_cap_stops_reading(worktree: Path, store: Store) -> None:
    base = _commit(worktree, {"README.md": README})
    first = _load(worktree, base, store, FakeModel(_answer(ok=False, error=AIErrorKind.EXIT,
                                                           data=None)), repo_daily=1)  # fmt: skip
    assert isinstance(first, ModelFailed)
    result = _load(worktree, base, store, FakeModel(), pr=2, repo_daily=1)
    assert isinstance(result, CapReached) and result.denied.cap == "repo_daily"


# --- 400KB를 넘는 규칙 문서 (DESIGN.md 6.3) ---

ORDER_RULE: Final = "`orders/` 아래 모듈은 `print`를 쓰지 않는다."
STYLE_RULE: Final = "함수 이름은 동사로 시작한다."
PAY_RULE: Final = "`payments/` 아래 모듈은 금액을 `Decimal`로 다룬다."
FILLER: Final = "이 절은 규칙이 아닌 설명이다. " * 40


def _big_readme(parts: int = 400) -> str:
    """앞에 머리와 코드 규칙 절, 가운데 설명 절이 많고, 주문·결제 절에 규칙이 하나씩 있는 문서."""
    filler = "".join(f"## 설명 {n}\n\n{FILLER}\n\n" for n in range(parts))
    return (
        "# 큰 안내서\n\n머리 글\n\n## 코드 규칙\n\n- " + STYLE_RULE + "\n\n" + filler
        + "## 주문 모듈\n\n- " + ORDER_RULE + "\n\n## 결제 모듈\n\n- " + PAY_RULE + "\n"
    )  # fmt: skip


class FakeSections:
    """similar의 글이 든 조각만 top-k로 돌려주는 가짜 검색."""

    def __init__(self, similar: dict[str, float]) -> None:
        self.similar = similar
        self.seen: list[tuple[list[str], str]] = []

    def find_sections(self, chunks, query: str):
        self.seen.append(([t.split("\n", 1)[0] for _, t in chunks], query))
        return [(i, s) for i, t in chunks for k, s in self.similar.items() if k in t]


ORDER_DIFF: Final = (
    b"diff --git a/orders/a.py b/orders/a.py\n--- a/orders/a.py\n+++ b/orders/a.py\n"
    b"@@ -1 +1,2 @@\n x = 1\n+print(x)\n"
)


def _load_big(worktree: Path, base: str, store: Store, model: FakeModel, sections):
    budget = CallBudget(store, "o/r", 1, "head", CallSettings(), now=lambda: NOW)
    return load_rules(worktree, base, RulesSettings(), model="haiku", store=store, budget=budget,
                      ask=model, sections=sections, changes_diff=ORDER_DIFF)  # fmt: skip


def test_big_document_reads_only_related_sections_and_caches_them(
    worktree: Path, store: Store
) -> None:
    readme = _big_readme()
    assert len(readme.encode()) > 400_000
    base = _commit(worktree, {"README.md": readme})
    sections = FakeSections({"## 주문 모듈": 0.95})
    model = FakeModel(_answer(_rule(STYLE_RULE, "코드 규칙"), _rule(ORDER_RULE, "주문 모듈")))
    result = _load_big(worktree, base, store, model, sections)

    assert isinstance(result, RulesFound) and not result.cached
    assert [r.text for r in result.rules] == [STYLE_RULE, ORDER_RULE]
    assert result.sections == ("README.md (머리)", "README.md > 코드 규칙", "README.md > 주문 모듈")
    assert result.rules[1].source.section == "주문 모듈"
    ((prompt, _),) = model.calls
    assert ORDER_RULE in prompt and STYLE_RULE in prompt and PAY_RULE not in prompt
    assert "설명 3" not in prompt and len(prompt.encode()) < 400_000
    ((_, query),) = sections.seen
    assert "orders/a.py" in query and "print(x)" in query
    # 같은 문서의 다음 검사는 읽어 둔 절을 다시 읽지 않는다
    again = _load_big(worktree, base, store, FakeModel(), FakeSections({"## 주문 모듈": 0.95}))
    assert isinstance(again, RulesFound) and again.cached
    assert [r.id for r in again.rules] == [r.id for r in result.rules]


def test_saved_section_whose_scope_covers_a_changed_file_is_read_without_search(
    worktree: Path, store: Store
) -> None:
    base = _commit(worktree, {"README.md": _big_readme()})
    scoped = {**_rule(ORDER_RULE, "주문 모듈"), "scope": ["orders/**"]}
    _load_big(worktree, base, store, FakeModel(_answer(scoped)), FakeSections({"## 주문": 0.95}))
    result = _load_big(worktree, base, store, FakeModel(), FakeSections({}))
    assert isinstance(result, RulesFound) and "README.md > 주문 모듈" in result.sections


def test_big_document_without_search_reads_nothing_beyond_the_limit(
    worktree: Path, store: Store
) -> None:
    base = _commit(worktree, {"README.md": _big_readme()})
    result = _load_big(worktree, base, store, FakeModel(), None)
    assert isinstance(result, NoRuleDocuments)
    assert "크기 상한을 넘음" in result.unread[0].reason


def test_failed_section_search_falls_back_to_the_size_limit(worktree: Path, store: Store) -> None:
    class Broken(FakeSections):
        def find_sections(self, chunks, query: str):
            raise RuntimeError("chroma 실패")

    base = _commit(worktree, {"README.md": _big_readme()})
    result = _load_big(worktree, base, store, FakeModel(), Broken({}))
    assert isinstance(result, NoRuleDocuments)


def test_small_documents_never_ask_the_section_search(worktree: Path, store: Store) -> None:
    base = _commit(worktree, {"README.md": README})
    sections = FakeSections({})
    model = FakeModel(_answer(_rule(PRINT_RULE, "코드 규칙 > 출력")))
    result = _load_big(worktree, base, store, model, sections)
    assert isinstance(result, RulesFound) and result.sections == ()
    assert sections.seen == []
