import json
from pathlib import Path

from app.worker import rule_reading_pure
from app.worker.ai.claude_cli import AIErrorKind, ClaudeResult, StructuredResult
from app.worker.fix import fix_decision_pure

from .model_memo import Memo, memo_key, stage_of


def _answer(data: dict | None, ok: bool = True) -> StructuredResult:
    return StructuredResult(ok=ok, error=None if ok else AIErrorKind.TIMEOUT, data=data,
                            model_id="claude-sonnet-x", input_tokens=10, output_tokens=2,
                            cost_usd=0.01, permission_denials=0)  # fmt: skip


class Asker:
    def __init__(self, *results: StructuredResult) -> None:
        self.results, self.prompts = list(results), []

    def __call__(self, prompt: str, **kw) -> StructuredResult:
        self.prompts.append(prompt)
        return self.results.pop(0)


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def _ask(run, prompt: str, schema=rule_reading_pure.ANSWER_SCHEMA, model: str = "sonnet"):
    return run(prompt, system_prompt="지시", schema=schema, model=model, max_budget_usd=1.0)


def test_same_input_is_asked_once_and_logged_as_a_hit(tmp_path: Path) -> None:
    path = tmp_path / "memo.jsonl"
    asker = Asker(_answer({"rules": []}), _answer({"rules": [1]}))
    run = Memo(path).structured(asker, lambda: "t::a")
    first, again = _ask(run, "README"), _ask(run, "README")
    other_model = _ask(run, "README", model="haiku")
    assert asker.prompts == ["README", "README"]
    assert first == again and other_model.data == {"rules": [1]}
    assert [(r["kind"], r["stage"], r["test"]) for r in _rows(path)] == [
        ("call", "rules", "t::a"),
        ("hit", "rules", "t::a"),
        ("call", "rules", "t::a"),
    ]


def test_answers_survive_in_the_file_and_failures_are_asked_again(tmp_path: Path) -> None:
    path = tmp_path / "memo.jsonl"
    failed, ok = _answer(None, ok=False), _answer({"decisions": []})
    run = Memo(path).structured(Asker(failed, ok), lambda: "t::a")
    assert not _ask(run, "v", fix_decision_pure.ANSWER_SCHEMA).ok
    assert _ask(run, "v", fix_decision_pure.ANSWER_SCHEMA).ok
    reopened = Memo(path).structured(Asker(), lambda: "t::b")
    assert _ask(reopened, "v", fix_decision_pure.ANSWER_SCHEMA) == ok
    rows = _rows(path)
    assert [r["kind"] for r in rows] == ["call", "call", "hit"]
    assert rows[0]["result"]["error"] == "timeout" and rows[0]["stage"] == "decide"


def test_fix_writing_is_logged_but_never_reused(tmp_path: Path) -> None:
    path = tmp_path / "memo.jsonl"
    calls = []

    def run_claude(work, prompt, **kw) -> ClaudeResult:
        calls.append(prompt)
        return ClaudeResult(ok=True, error=None, cost_usd=0.0, num_turns=3, permission_denials=(),
                            text="{}", model_id="claude-haiku-x")  # fmt: skip

    run = Memo(path).fix(run_claude, lambda: "t::fix")
    run(tmp_path, "고쳐", model="haiku", max_budget_usd=1.0)
    run(tmp_path, "고쳐", model="haiku", max_budget_usd=1.0)
    assert calls == ["고쳐", "고쳐"]
    assert [(r["kind"], r["model"], r["num_turns"]) for r in _rows(path)] == [
        ("fix", "haiku", 3),
        ("fix", "haiku", 3),
    ]


def test_key_and_stage() -> None:
    assert memo_key("p", "s", {"a": 1}, "m") == memo_key("p", "s", {"a": 1}, "m")
    assert memo_key("p", "s", {"a": 1}, "m") != memo_key("p", "s2", {"a": 1}, "m")
    assert stage_of(rule_reading_pure.ANSWER_SCHEMA) == "rules"
    assert stage_of({"type": "object"}) == "other" and stage_of(None) == "other"
