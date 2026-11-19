"""worker/rules.py: base의 문서로 규칙 묶음을 만든다. 로컬 git, SQLite, 가짜 모델로 확인한다."""

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import fakeredis
import pytest

from app.models.call_budget import Denied
from app.models.repo_config import CallSettings, RulesSettings
from app.models.rule_reading import ANSWER_SCHEMA, SYSTEM_PROMPT
from app.worker import repo
from app.worker.ai.claude_cli import AIErrorKind, StructuredResult
from app.worker.budget import CallBudget
from app.worker.rules import (
    CapReached,
    ModelFailed,
    NoRuleDocuments,
    NoRules,
    RulesFound,
    load_rules,
)
from app.worker.state import Store

README: Final = (Path(__file__).parent.parent / "fixtures" / "rules" / "README.md").read_text()
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
