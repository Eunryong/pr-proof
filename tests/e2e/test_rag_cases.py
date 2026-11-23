"""과거 기록 검색 E2E (eval/rag/*.yaml, 13절 5단계의 5-1–5-13).

문서 규칙 케이스와 같은 방식이다: 예제 리포의 로컬 git 원격, 실제 샌드박스 테스트, 실제 ast-grep,
실제 Chroma(데이터 폴더의 `chroma/`), GitHub만 가짜. 모델은 케이스의 답을 돌려주는 가짜이고, 임베딩
모델도 기본으로 가짜(글자 3-gram, tests/worker/rag_helpers.py)라 `pytest -m docker`가 결정적이고
470MB를 받지 않는다. `PR_PROOF_REAL_EMBED=1`과 `PR_PROOF_EMBED_DIR`(고정한 판의
multilingual-e5-small 파일이 든 폴더)을 주면 실제 임베딩 모델을 쓴다. 그때
`fake_embed_only: true`인 케이스는 건너뛴다(가짜 모델의 유사도에 맞춘 기준값을 쓰는 케이스).

케이스 형식은 문서 규칙 케이스에 아래를 더한다.

    records: 검사 전에 SQLite에 넣을 과거 기록. rule은 `model.rules`의 순번(그 규칙의 원문과 id를
        쓴다)이고, 없으면 rule_text·rule_id를 그대로 쓴다. repo를 주면 그 저장소의 기록이다
    expect.prompt_records: {decide, fix} 그 단계의 프롬프트 하나에 들어간 기록 수의 최댓값
    expect.chroma, models_dir: 데이터 폴더에 `chroma/`, `models/`가 있어야 하는지
    expect.embedded: 이 저장소 기록 중 임베딩된 수. other_embedded: 다른 저장소 기록 중 임베딩된 수
    expect.doc_collection: 이 저장소의 문서 조각 컬렉션이 있어야 하는지
    expect.blocked_imports: 검사 동안 chromadb·onnxruntime·tokenizers를 import하지 못하게 막는다
    expect.reindex_same: 검사 뒤 벡터 저장소를 지우고 `reindex`한 뒤 `/recheck`의 참고 기록이 같다
"""

import hashlib
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Final

import pytest
import yaml

from app.shared.embed_models_pure import E5_SMALL, EMBED_MODELS
from app.shared.job_pure import PRJob
from app.worker import main
from app.worker.ai import claude_cli
from app.worker.cli import reindex
from app.worker.core.rule_pure import rule_id
from app.worker.fix import fix_decision_pure
from app.worker.store import memory
from app.worker.store.rag_pure import collection_name
from app.worker.store.records_pure import NewRecord

from ..worker.store.rag_helpers import FAKE_SPEC, FakeEncoder
from .test_lint_cases import _bodies, _git, shared_venvs  # noqa: F401
from .test_rules_cases import (
    REAL_MODEL_ENV,
    _check,
    _check_fix,
    _remote,
    deps_for,  # noqa: F401
    doc_rules_section,
    flagged,
    scripted_fixer,
    scripted_model,
)

CASES_DIR: Final = Path(__file__).resolve().parents[2] / "eval" / "rag"
CASES: Final = sorted(CASES_DIR.glob("*.yaml"))
REPO: Final = "o/r"
REAL_EMBED_ENV: Final = "PR_PROOF_REAL_EMBED"
EMBED_DIR_ENV: Final = "PR_PROOF_EMBED_DIR"
HEAVY: Final = ("chromadb", "onnxruntime", "tokenizers")
_RECORD_LINE: Final = re.compile(r"^기록 \d+ · ", re.M)
_REFERENCES: Final = re.compile(r"<details><summary>참고한 과거 기록.*?</details>", re.S)


def real_embed() -> bool:
    return os.environ.get(REAL_EMBED_ENV) == "1"


def use_embedder(monkeypatch) -> None:
    """검색의 임베딩 모델: 기본은 가짜, PR_PROOF_REAL_EMBED=1이면 받아 둔 실제 모델 폴더."""
    if real_embed():
        folder = Path(os.environ[EMBED_DIR_ENV])
        spec = EMBED_MODELS[E5_SMALL]
        for f in (spec.tokenizer, spec.onnx):
            data = (folder / Path(f.path).name).read_bytes()
            assert hashlib.sha256(data).hexdigest() == f.sha256, f.path
        monkeypatch.setattr(memory, "ensure_model", lambda *a, **k: folder)
        return

    def open_retriever(data_dir, db, repo, settings, spec, download=None):
        vectors = memory.VectorStore(data_dir / memory.CHROMA_DIR, FAKE_SPEC.name, FAKE_SPEC.dim)
        return memory.Retriever(db, repo, settings, vectors, FakeEncoder())

    monkeypatch.setattr(memory, "open_retriever", open_retriever)


def encoder_for_reindex():
    if real_embed():
        spec = EMBED_MODELS[E5_SMALL]
        return memory.OnnxEncoder(spec, Path(os.environ[EMBED_DIR_ENV]))
    return FakeEncoder()


def expand(edits: list[dict] | None) -> list[dict]:
    """`create_parts`(글 조각과 {repeat: {times, text}})를 `create` 하나로 편다. 400KB가 넘는 규칙
    문서를 케이스 파일에 그대로 적지 않으려는 것이다. text의 {n}은 반복 번호다."""
    out = []
    for edit in edits or ():
        if "create_parts" not in edit:
            out.append(edit)
            continue
        text = ""
        for part in edit["create_parts"]:
            if isinstance(part, str):
                text += part
            else:
                rep = part["repeat"]
                text += "".join(rep["text"].replace("{n}", str(n)) for n in range(rep["times"]))
        out.append({"file": edit["file"], "create": text})
    return out


def seed(case: dict, store) -> None:
    rules = case["model"]["rules"]
    for n, r in enumerate(case.get("records") or ()):
        quote = rules[r["rule"]]["quote"] if "rule" in r else r["rule_text"]
        store.add_record(NewRecord(
            repo=r.get("repo", REPO), kind=r["kind"], origin=r.get("origin", "fix_pr"),
            source_id=f"eval:{n}", rule_id=r.get("rule_id") or rule_id("README.md", quote),
            rule_text=quote, file=r.get("file", ""), line_text=r.get("line_text", ""),
            context=r.get("context", ""), diff=r.get("diff", ""), detail=r.get("detail", ""),
            pr_number=r.get("pr_number"), fix_pr_number=r.get("fix_pr_number"),
        ))  # fmt: skip


class Prompts:
    """단계별로 모델에 간 프롬프트. 수정 판단은 가짜 모델을 감싸 남기고, 수정 작성은 가짜 수정
    모델이 남긴 것을 읽는다."""

    def __init__(self, ask) -> None:
        self.ask, self.decide = ask, []

    def __call__(self, prompt: str, *, schema, **kw):
        if schema is fix_decision_pure.ANSWER_SCHEMA:
            self.decide.append(prompt)
        return self.ask(prompt, schema=schema, **kw)


def _most_records(prompts: list[str]) -> int:
    return max((len(_RECORD_LINE.findall(p)) for p in prompts), default=0)


def _collections(data: Path) -> set[str]:
    import chromadb
    from chromadb.config import Settings

    client = chromadb.PersistentClient(path=str(data / memory.CHROMA_DIR),
                                       settings=Settings(anonymized_telemetry=False))  # fmt: skip
    return {c.name for c in client.list_collections()}


@pytest.mark.docker
@pytest.mark.parametrize("case_path", CASES, ids=[p.stem for p in CASES])
def test_rag_case(case_path: Path, eval_repo: Path, deps_for, tmp_path: Path,  # noqa: F811
                  monkeypatch) -> None:  # fmt: skip
    case = yaml.safe_load(case_path.read_text())
    case["base"] = expand(case.get("base"))
    expect = case["expect"]
    if os.environ.get(REAL_MODEL_ENV) == "1":
        pytest.skip("검색 케이스는 가짜 모델로 연결을 본다. 실제 모델 비교는 test_rag_compare.py")
    if real_embed() and case.get("fake_embed_only"):
        pytest.skip("가짜 임베딩 모델의 유사도에 맞춘 케이스")
    if not expect.get("blocked_imports"):
        pytest.importorskip("chromadb")
    fixer = scripted_fixer(case) if "fix" in case else None
    prompts = Prompts(scripted_model(case))
    monkeypatch.setattr(claude_cli, "run_structured", prompts)
    if fixer is not None:
        monkeypatch.setattr(claude_cli, "run_claude", fixer)
    use_embedder(monkeypatch)
    if expect.get("blocked_imports"):
        for name in HEAVY:
            monkeypatch.setitem(sys.modules, name, None)
    remote = _remote(tmp_path, eval_repo, case)
    deps, gh, redis = deps_for(remote)
    seed(case, deps.store)
    data = deps.settings.data_dir

    section = doc_rules_section(_check(deps, gh, redis, remote, pr=1))
    assert flagged(section) == sorted(expect["violations"]), section
    for text in expect.get("contains", ()):
        assert text in section, (text, section)
    for text in expect.get("absent", ()):
        assert text not in section, (text, section)
    if "fix" in expect:
        _check_fix(expect, section, gh, remote, fixer, real_model=False)
    if "prompt_records" in expect:
        want = expect["prompt_records"]
        got = {"decide": _most_records(prompts.decide),
               "fix": _most_records(fixer.calls if fixer is not None else [])}  # fmt: skip
        assert {k: got[k] for k in want} == want, (got, prompts.decide)
    if "chroma" in expect:
        assert (data / memory.CHROMA_DIR).is_dir() == expect["chroma"]
    if "models_dir" in expect:
        assert (data / memory.MODEL_DIR).exists() == expect["models_dir"]
    if "embedded" in expect:
        rows = [r for r in deps.store.records(REPO) if r.source_id.startswith("eval:")]
        assert sum(r.embedded_at is not None for r in rows) == expect["embedded"]
    if "other_embedded" in expect:
        others = [r for repo in deps.store.record_repos() if repo != REPO
                  for r in deps.store.records(repo)]  # fmt: skip
        assert sum(r.embedded_at is not None for r in others) == expect["other_embedded"]
    if "doc_collection" in expect:
        names = _collections(data)
        assert (collection_name(REPO, "docs") in names) == expect["doc_collection"], names
    if expect.get("reindex_same"):
        _check_reindex(deps, gh, section, data)


def _check_reindex(deps, gh, section: str, data: Path) -> None:
    """5-4: 벡터 저장소를 지우고 SQLite의 기록으로 다시 만든 뒤 같은 커밋을 `/recheck`하면 참고한
    기록(번호와 유사도)이 같다."""
    before = _REFERENCES.search(section)
    assert before is not None, section
    from chromadb.api.client import SharedSystemClient

    SharedSystemClient.clear_system_cache()
    shutil.rmtree(data / memory.CHROMA_DIR)
    assert reindex(deps.settings, [REPO], encoder=encoder_for_reindex()) == 0
    job = PRJob(repo=REPO, pr_number=1, kind="command", command="/recheck", comment_id=72,
                source="webhook")  # fmt: skip
    assert main.handle(job.model_dump_json(), deps).startswith("recheck:")
    after = _REFERENCES.search(doc_rules_section(_bodies(gh)[-1]))
    assert after is not None, _bodies(gh)[-1]

    def decide_lines(block: str) -> list[str]:
        # 같은 커밋의 수정안 PR이 열려 있어 `/recheck`는 수정 작성을 다시 하지 않는다
        return [line for line in block.splitlines() if "수정 판단" in line]

    assert decide_lines(after.group(0)) == decide_lines(before.group(0)) != []


def test_case_files_are_well_formed() -> None:
    assert CASES
    for path in CASES:
        case = yaml.safe_load(path.read_text())
        assert {"name", "description", "pr", "model", "expect"} <= case.keys(), path
        for record in case.get("records") or ():
            assert record["kind"] in {"adopted", "rejected", "failed", "checker"}, path
            assert ("rule" in record) != ("rule_text" in record), path
