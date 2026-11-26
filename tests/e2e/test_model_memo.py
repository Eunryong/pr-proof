"""호출 기록(`model_memo.py`)의 부품: 저장 키, 단계 이름, 기록한 답을 다시 쓰는 일. 모델을 부르지
않고 결과 값으로 본다."""

import json
from pathlib import Path

from app.worker.model.claude.claude_cli import AIErrorKind, StructuredResult
from app.worker.rules import rule_reading_pure

from .model_memo import Memo, memo_key, stage_of


def _result(data: dict | None, ok: bool = True) -> StructuredResult:
    return StructuredResult(
        ok=ok, error=None if ok else AIErrorKind.TIMEOUT, data=data, model_id="m",
        input_tokens=1, output_tokens=1, cost_usd=0.0, permission_denials=0,
    )  # fmt: skip


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_a_recorded_answer_is_reused_and_logged_as_a_hit(tmp_path: Path) -> None:
    path = tmp_path / "memo.jsonl"
    memo = Memo(path)
    answer = _result({"rules": []})
    assert memo.hit("k", "rules", "sonnet", "t::a") is None
    memo.called("k", "rules", "sonnet", "t::a", 1.5, answer)
    assert memo.hit("k", "rules", "sonnet", "t::a") == answer
    assert memo.hit("other", "rules", "haiku", "t::a") is None
    assert [(r["kind"], r["stage"], r["test"]) for r in _rows(path)] == [
        ("call", "rules", "t::a"),
        ("hit", "rules", "t::a"),
    ]


def test_answers_survive_in_the_file_and_failures_are_not_reused(tmp_path: Path) -> None:
    path = tmp_path / "memo.jsonl"
    memo = Memo(path)
    ok = _result({"decisions": []})
    memo.called("failed", "decide", "sonnet", "t::a", 0.1, _result(None, ok=False))
    memo.called("ok", "decide", "sonnet", "t::a", 0.1, ok)
    assert memo.hit("failed", "decide", "sonnet", "t::a") is None
    reopened = Memo(path)
    assert reopened.hit("ok", "decide", "sonnet", "t::b") == ok
    assert reopened.hit("failed", "decide", "sonnet", "t::b") is None
    rows = _rows(path)
    assert [r["kind"] for r in rows] == ["call", "call", "hit"]
    assert rows[0]["result"]["error"] == "timeout" and rows[0]["stage"] == "decide"


def test_key_and_stage() -> None:
    assert memo_key("p", "s", {"a": 1}, "m") == memo_key("p", "s", {"a": 1}, "m")
    assert memo_key("p", "s", {"a": 1}, "m") != memo_key("p", "s2", {"a": 1}, "m")
    assert stage_of(rule_reading_pure.ANSWER_SCHEMA) == "rules"
    assert stage_of({"type": "object"}) == "other" and stage_of(None) == "other"
