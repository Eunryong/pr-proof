"""검사기 작성에 검색을 켰을 때와 껐을 때 (docs/DESIGN.md 6.2, 완료 기준 5-14). 도구를 끈 평가와
같은 표본(eval/tools_off.yaml의 eval 저장소: 개념 PLR2004·PLW0603·T201, ruff가 정답지)이다.

- 검색 끔: 빈 DB에서 규칙을 한 번 읽고 개념 규칙마다 검사기를 쓴다(도구 목록 없음).
- 검색 켬: 규칙마다 빈 DB를 따로 두고, 검색 끔이 채택한 다른 개념 규칙의 검사기 기록만 넣은 뒤(같은
  규칙의 기록은 넣지 않는다: 그것은 같은 규칙의 검사기를 다시 쓰는 것이라 "비슷한 규칙"이 아니다) 그
  규칙의 검사기를 쓴다. 규칙 읽기의 결과는 검색 끔의 것을 그대로 쓴다.

재현율은 개념 규칙의 검사기가 표본의 바뀐 줄에서 잡은 줄 중 ruff 정답의 비율이다. 검사기를 만들지
못한 규칙은 모델 판단으로 내려가지만 여기서는 모델 판단을 돌리지 않는다(검사기 작성만 비교한다).
평균 회차는 `checkers`에 남은 회차 기록의 수다. 프롬프트에 넣은 기록의 수와 유사도도 남긴다.

기본은 가짜 모델과 가짜 임베딩 모델(기준값 0)로 한 번 돌아 하네스만 본다. `PR_PROOF_REAL_MODEL=1`과
`PR_PROOF_REAL_EMBED=1`(`PR_PROOF_EMBED_DIR`)이면 실제 모델로 `PR_PROOF_RAG_CHECKER_RUNS`(기본 1)번
돌고, `PR_PROOF_RAG_CHECKER_OUT`이 있으면 결과 JSON을 쓴다. 기준값은 저장소 설정의 기본값이다.
"""

import json
import os
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Final

import pytest

from app.shared.embed_models_pure import E5_SMALL, EMBED_MODELS
from app.worker.config.repo_config_pure import ModelSettings, RagSettings, RulesSettings
from app.worker.model.claude import claude_cli
from app.worker.model.claude.claude_backend import ClaudeCliBackend
from app.worker.rules import checkers, doc_rules
from app.worker.rules.checker_authoring_pure import checker_key
from app.worker.rules.doc_rules_pure import Found, RuleChecked, RulesRead
from app.worker.store import memory
from app.worker.store.budget import CallBudget
from app.worker.store.records_pure import NewRecord
from app.worker.store.state import Store
from tests.sandbox_names import TEST_NAMES

from ..redis_stand_ins import MemoryKeys
from ..worker.store.rag_helpers import FAKE_SPEC, FakeEncoder
from .test_rag_cases import EMBED_DIR_ENV, real_embed
from .test_rules_cases import REAL_MODEL_ENV
from .test_tools_off_eval import EVAL_CALLS, Calls, Target, _scripted, _truth, eval_target

RUNS_ENV: Final = "PR_PROOF_RAG_CHECKER_RUNS"
OUT_ENV: Final = "PR_PROOF_RAG_CHECKER_OUT"


def _encoder():
    if real_embed():
        return memory.OnnxEncoder(EMBED_MODELS[E5_SMALL], Path(os.environ[EMBED_DIR_ENV]))
    return FakeEncoder()


def _settings() -> RagSettings:
    return (
        RagSettings(enabled=True) if real_embed() else RagSettings(enabled=True, min_similarity=0.0)
    )


def _runtime(store: Store, target: Target, ask, sandbox, retriever=None) -> doc_rules.Runtime:
    budget = CallBudget(store, f"local/{target.name}", 1, target.base_sha, EVAL_CALLS)
    return doc_rules.Runtime(store=store, budget=budget, models=ModelSettings(),
                             backend=ClaudeCliBackend(run_structured=ask),
                             sandbox=sandbox, retriever=retriever)  # fmt: skip


def _score(read: RulesRead, outcomes, target: Target, rt: doc_rules.Runtime) -> set[tuple]:
    found: set[tuple] = set()
    for sample in target.samples:
        got = doc_rules.find(read, outcomes, sample.root, sample.diff.encode(), rt, since_call=0)
        assert isinstance(got, Found), got
        found |= {(v.rule_id, (sample.name, v.file, v.line)) for v in got.violations}
    return found


def _attempts(store: Store, rule) -> int:
    entry = store.saved_checker(checker_key(rule, ModelSettings().checker))
    return len(json.loads(entry.attempts_json)) if entry is not None else 0


def run_once(target: Target, ask: Callable, data: Path, tool_cache: Path) -> dict:
    calls = Calls(ask)
    sandbox = checkers.Sandbox(tool_cache, image=TEST_NAMES.image)
    off_store = Store(data / "off.db", MemoryKeys())
    off = _runtime(off_store, target, calls, sandbox)
    read = doc_rules.read(target.base_root, target.base_sha, RulesSettings(), off)
    assert isinstance(read, RulesRead), read
    wanted = tuple(r for r in read.rules if any(c.needle in r.text for c in target.concepts))
    calls.arm = "off"
    read = replace(read, rules=wanted)
    outcomes = doc_rules.author(read, off)
    assert isinstance(outcomes, tuple), outcomes
    arms = {"off": {"found": _score(read, outcomes, target, off), "outcomes": outcomes,
                    "attempts": {r.id: _attempts(off_store, r) for r in wanted},
                    "records": {r.id: [] for r in wanted}}}  # fmt: skip
    history = [r for r in off_store.records(f"local/{target.name}") if r.kind == "checker"]
    calls.arm = "on"
    on = {"found": set(), "outcomes": (), "attempts": {}, "records": {}}
    for n, rule in enumerate(wanted):
        store = Store(data / f"on-{n}.db", MemoryKeys())
        for r in history:
            if r.rule_id != rule.id:
                store.add_record(NewRecord(
                    repo=r.repo, kind=r.kind, origin=r.origin, source_id=r.source_id,
                    rule_id=r.rule_id, rule_text=r.rule_text, file=r.file, line_text=r.line_text,
                    context=r.context, diff=r.diff,
                ))  # fmt: skip
        vectors = memory.VectorStore(data / f"chroma-{n}", *_dims())
        retriever = memory.Retriever(
            store, f"local/{target.name}", _settings(), vectors, _encoder()
        )
        rt = _runtime(store, target, calls, sandbox, retriever)
        used = []
        one = replace(read, rules=(rule,))
        got = doc_rules.author(one, rt, on_retrieval=used.append)
        assert isinstance(got, tuple), got
        on["found"] |= _score(one, got, target, rt)
        on["outcomes"] += got
        on["attempts"][rule.id] = _attempts(store, rule)
        on["records"][rule.id] = [(ref.record_id, ref.rule_id, round(ref.similarity, 3))
                                  for u in used for ref in u.references]  # fmt: skip
        on["records"][rule.id + ":below"] = sum(u.below for u in used)
        vectors.close()
    arms["on"] = on
    scores = []
    for concept in target.concepts:
        truth = _truth(target, concept, sandbox)
        rules = [r for r in wanted if concept.needle in r.text]
        for arm, result in arms.items():
            ids = {r.id for r in rules}
            hits = {h for rid, h in result["found"] if rid in ids}
            methods = {
                o.rule.id: o.checker.kind if isinstance(o, RuleChecked) else type(o).__name__
                for o in result["outcomes"] if o.rule.id in ids
            }  # fmt: skip
            scores.append({
                "concept": concept.code, "arm": arm,
                "recall": f"{len(truth & hits)}/{len(truth)}",
                "precision": f"{len(truth & hits)}/{len(hits)}",
                "attempts": [result["attempts"][r.id] for r in rules], "methods": methods,
                "records": {r.id: result["records"].get(r.id, []) for r in rules},
                "below": {r.id: result["records"].get(r.id + ":below", 0) for r in rules},
            })  # fmt: skip
    return {"scores": scores, "calls": calls.log}


def _dims() -> tuple[str, int]:
    spec = EMBED_MODELS[E5_SMALL] if real_embed() else FAKE_SPEC
    return spec.name, spec.dim


@pytest.mark.docker
def test_checker_authoring_with_and_without_search(
    eval_repo: Path, sandbox_image: str, tool_cache: Path, tmp_path: Path
) -> None:
    pytest.importorskip("chromadb")
    real = os.environ.get(REAL_MODEL_ENV) == "1"
    if real and not real_embed():
        pytest.skip("실제 모델의 비교는 실제 임베딩 모델로 한다 (PR_PROOF_REAL_EMBED=1)")
    target = eval_target(eval_repo, tmp_path / "eval")
    runs = []
    for n in range(int(os.environ.get(RUNS_ENV, "1")) if real else 1):
        data = tmp_path / f"data-{n}"
        data.mkdir()
        ask = claude_cli.run_structured if real else _scripted(target)
        runs.append({"rep": n + 1, **run_once(target, ask, data, tool_cache)})
    for run in runs:
        for s in run["scores"]:
            print(run["rep"], s["concept"], s["arm"], s["recall"], s["precision"],
                  s["attempts"], s["methods"], s["records"], s["below"])  # fmt: skip
        print(run["rep"], [(c["arm"], c["stage"], c["model_id"]) for c in run["calls"]])
    if os.environ.get(OUT_ENV):
        out = Path(os.environ[OUT_ENV])
        out.mkdir(parents=True, exist_ok=True)
        (out / "rag_checker.json").write_text(json.dumps(runs, ensure_ascii=False, indent=1))
    if real:
        return
    (run,) = runs
    by = {(s["concept"], s["arm"]): s for s in run["scores"]}
    for code in ("PLR2004", "PLW0603", "T201"):
        # 가짜 모델은 검색과 관계없이 같은 검사기를 쓰므로 두 조건의 점수가 같다
        assert by[(code, "on")]["recall"] == by[(code, "off")]["recall"]
        # 검색 켬에는 다른 개념 규칙의 검사기 기록만 들어간다 (기준값 0)
        records = [x for rows in by[(code, "on")]["records"].values() for x in rows]
        assert records and all(rule_id not in by[(code, "on")]["records"]
                               for _, rule_id, _ in records)  # fmt: skip
        assert all(not rows for rows in by[(code, "off")]["records"].values())
    assert {c["stage"] for c in run["calls"] if c["arm"] == "on"} == {"checker"}
