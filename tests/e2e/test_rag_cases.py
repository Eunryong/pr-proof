"""과거 기록 검색 E2E (eval/rag/*.yaml, 13절 5단계의 5-1–5-13).

문서 규칙 케이스와 같은 방식이다: 예제 리포의 로컬 git 원격, 실제 샌드박스 테스트, 실제 ast-grep,
실제 Chroma(데이터 폴더의 `chroma/`), GitHub만 가짜. 설정된 모델을 실제로 부르므로
`PR_PROOF_REAL_MODEL=1`일 때만 돈다. 임베딩도 실제 모델이다: `PR_PROOF_REAL_EMBED=1`과
`PR_PROOF_EMBED_DIR`(고정한 판의 multilingual-e5-small 파일이 든 폴더)이 있어야 한다. 검색을 쓰지
않는 것을 보는 케이스(`expect.blocked_imports`)는 임베딩 모델 없이 돈다.

케이스 형식은 문서 규칙 케이스에 아래를 더한다.

    records: 검사 전에 SQLite에 넣을 과거 기록. rule_text는 규칙의 원문이고 rule_id가 없으면 그
        원문으로 만든다. repo를 주면 그 저장소의 기록이다
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
from app.worker.cli import reindex
from app.worker.core.rule_pure import rule_id
from app.worker.fix import fix_decision_pure
from app.worker.model.claude import claude_cli
from app.worker.store import memory
from app.worker.store.rag_pure import collection_name
from app.worker.store.records_pure import NewRecord

from .conftest import needs_model
from .test_lint_cases import _bodies, _git, shared_venvs  # noqa: F401
from .test_rules_cases import (
    _check,
    _check_fix,
    _remote,
    deps_for,  # noqa: F401
    doc_rules_section,
    flagged,
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


def needs_embedder() -> None:
    if not real_embed():
        pytest.skip(f"임베딩 모델을 쓰는 테스트 ({REAL_EMBED_ENV}=1, {EMBED_DIR_ENV})")


def use_embedder(monkeypatch) -> None:
    """검색이 받아 둔 임베딩 모델 폴더(`PR_PROOF_EMBED_DIR`)를 쓰게 한다. 다시 받지 않는다."""
    folder = Path(os.environ[EMBED_DIR_ENV])
    spec = EMBED_MODELS[E5_SMALL]
    for f in (spec.tokenizer, spec.onnx):
        data = (folder / Path(f.path).name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == f.sha256, f.path
    monkeypatch.setattr(memory, "ensure_model", lambda *a, **k: folder)


def encoder_for_reindex():
    return memory.OnnxEncoder(EMBED_MODELS[E5_SMALL], Path(os.environ[EMBED_DIR_ENV]))


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
    for n, r in enumerate(case.get("records") or ()):
        quote = r["rule_text"]
        store.add_record(NewRecord(
            repo=r.get("repo", REPO), kind=r["kind"], origin=r.get("origin", "fix_pr"),
            source_id=f"eval:{n}", rule_id=r.get("rule_id") or rule_id("README.md", quote),
            rule_text=quote, file=r.get("file", ""), line_text=r.get("line_text", ""),
            context=r.get("context", ""), diff=r.get("diff", ""), detail=r.get("detail", ""),
            pr_number=r.get("pr_number"), fix_pr_number=r.get("fix_pr_number"),
        ))  # fmt: skip


class Prompts:
    """수정 판단과 수정 작성의 모델 호출에 간 프롬프트를 남긴다. 호출은 그대로 넘긴다."""

    def __init__(self, ask, write) -> None:
        self._ask, self._write = ask, write
        self.decide: list[str] = []
        self.fix: list[str] = []

    def ask(self, prompt: str, *, schema, **kw):
        if schema is fix_decision_pure.ANSWER_SCHEMA:
            self.decide.append(prompt)
        return self._ask(prompt, schema=schema, **kw)

    def write(self, work, prompt: str, **kw):
        self.fix.append(prompt)
        return self._write(work, prompt, **kw)


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
    needs_model()
    case = yaml.safe_load(case_path.read_text())
    case["base"] = expand(case.get("base"))
    expect = case["expect"]
    prompts = Prompts(claude_cli.run_structured, claude_cli.run_claude)
    monkeypatch.setattr(claude_cli, "run_structured", prompts.ask)
    monkeypatch.setattr(claude_cli, "run_claude", prompts.write)
    if expect.get("blocked_imports"):
        for name in HEAVY:
            monkeypatch.setitem(sys.modules, name, None)
    else:
        needs_embedder()
        pytest.importorskip("chromadb")
        use_embedder(monkeypatch)
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
        _check_fix(expect, section, gh, remote, deps)
    if "prompt_records" in expect:
        want = expect["prompt_records"]
        got = {"decide": _most_records(prompts.decide), "fix": _most_records(prompts.fix)}
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
        assert {"name", "description", "pr", "expect"} <= case.keys(), path
        assert not {"model", "fix"} & case.keys(), path  # 모델의 답을 케이스에 적지 않는다
        for record in case.get("records") or ():
            assert record["kind"] in {"adopted", "rejected", "failed", "checker"}, path
            assert "rule_text" in record, path
