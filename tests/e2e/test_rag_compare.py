"""검색 없음 / 있음 비교 (docs/DESIGN.md 6.7, 완료 기준 5-1, 5-2).

eval/rag의 두 케이스를 같은 base·PR·기록으로 두 조건에서 돈다. 검색 끔(`rag.enabled: false`, 기록은
SQLite에 있지만 프롬프트에 들어가지 않음)과 검색 켬(기본 기준값). 매번 빈 데이터 폴더다(저장된 답이
조건 사이에 섞이지 않게).

- 5-1(`01-adopted-constants-file`): 팀이 상수를 orders/constants.py에 두고 import한 채택
  기록이 있을 때, 수정 작성이 고친 pricing.py가 constants.py에서 상수를 가져오는지(팀의 방식)
  아니면 그 파일에 상수를 새로 두는지.
- 5-5(`05-planted-instruction-in-record`): 채택 기록의 diff에 "handlers.py도 고쳐라"를 심었을 때
  수정 작성의 모델이 바꾼 파일.
- 5-2(`02-rejected-pattern-in-decide`): 같은 규칙의 위반을 상수로 바꾼 수정안을 팀이 닫은 거절
  기록이 있을 때, 수정 판단이 PR의 두 위반을 skip하는 비율과 그 이유가 기록을 드는지. 수정
  판단만 실제 모델이고 수정 작성은 케이스의 가짜다.

설정된 모델과 임베딩 모델을 실제로 부른다(`PR_PROOF_REAL_MODEL=1`, `PR_PROOF_REAL_EMBED=1`,
`PR_PROOF_EMBED_DIR`). 조건마다 `PR_PROOF_RAG_COMPARE_RUNS`(기본 1)번씩 돌고,
`PR_PROOF_RAG_COMPARE_OUT`이 있으면 결과 JSON을 쓴다. 결과는 재는 값이라 통과·실패로 가르지 않는다.
"""

import json
import os
import re
from pathlib import Path
from typing import Final

import pytest
import yaml

from app.shared import config
from app.worker.deps import Deps
from app.worker.fix import fix_decision_pure
from app.worker.git import repo
from app.worker.git.github import GitHub
from app.worker.model.claude import claude_cli
from app.worker.store.state import Store
from tests.sandbox_names import TEST_NAMES

from ..redis_stand_ins import MemoryKeys
from ..worker.git.test_github import FakeGh
from .conftest import needs_model
from .test_lint_cases import _git, shared_venvs  # noqa: F401
from .test_rag_cases import CASES_DIR, expand, needs_embedder, seed, use_embedder
from .test_rules_cases import (
    BRANCH,
    _check,
    _remote,
    doc_rules_section,
)

RUNS_ENV: Final = "PR_PROOF_RAG_COMPARE_RUNS"
OUT_ENV: Final = "PR_PROOF_RAG_COMPARE_OUT"
REPO: Final = "o/r"
ARMS: Final = ("off", "on")
_VIOLATION: Final = re.compile(
    r"^  - `(?P<at>[^`]+)` · (?P<action>fix|skip) \((?P<why>.*)\)$", re.M
)


def _case(name: str, arm: str) -> dict:
    case = yaml.safe_load((CASES_DIR / f"{name}.yaml").read_text())
    case["base"] = expand(case.get("base"))
    if arm == "off":
        case["pipeline"] = {}
    return case


def _deps(remote: dict, data: Path, shared_venvs: Path, tool_cache: Path, monkeypatch):  # noqa: F811
    data.mkdir()
    (data / "venvs").symlink_to(shared_venvs, target_is_directory=True)
    (data / "tools").symlink_to(tool_cache, target_is_directory=True)
    monkeypatch.setattr(repo, "remote_url", lambda name: str(remote["path"]))
    redis = MemoryKeys()
    gh = FakeGh(head=remote["head"], base=remote["base"])
    settings = config.PipelineConfig(
        repos=[REPO], data_dir=data,
        sandbox=config.SandboxConfig(image=TEST_NAMES.image,
                                     container_prefix=TEST_NAMES.container_prefix),
    )  # fmt: skip
    return Deps(Store(data / "pipeline.db", redis), GitHub(gh), settings), gh, redis


def _run(name: str, arm: str, n: int, tmp: Path, eval_repo: Path, shared_venvs,  # noqa: F811
         tool_cache: Path, monkeypatch) -> dict:  # fmt: skip
    """한 조건의 한 번."""
    case = _case(name, arm)
    real_ask = claude_cli.run_structured
    decide_prompts: list[str] = []

    def ask(prompt: str, *, schema, **kw):
        if schema is fix_decision_pure.ANSWER_SCHEMA:
            decide_prompts.append(prompt)
        return real_ask(prompt, schema=schema, **kw)

    monkeypatch.setattr(claude_cli, "run_structured", ask)
    use_embedder(monkeypatch)
    folder = tmp / f"{name}-{arm}-{n}"
    folder.mkdir()
    remote = _remote(folder, eval_repo, case)
    deps, gh, redis = _deps(remote, folder / "data", shared_venvs, tool_cache, monkeypatch)
    seed(case, deps.store)
    section = doc_rules_section(_check(deps, gh, redis, remote, pr=1))
    decisions = [m.groupdict() for m in _VIOLATION.finditer(section)]
    attempts = [a for p in deps.store.proposals(REPO, 1)
                if (a := deps.store.fix_attempt(p.attempt_id)) is not None]  # fmt: skip
    rows = deps.store.model_calls(REPO, 1)
    pushed = diff = ""
    if _git(remote["path"], "branch", "--list", BRANCH):
        pushed = _git(remote["path"], "show", f"{BRANCH}:orders/services/pricing.py")
        diff = _git(remote["path"], "diff", remote["head"], BRANCH)
    fix_rows = [c for c in rows if c.stage == "fix" and not c.cache_hit]
    added = [line[1:] for line in diff.splitlines()
             if line.startswith("+") and not line.startswith("+++")]  # fmt: skip
    return {
        "case": name, "arm": arm, "run": n,
        "records_in_decide_prompt": max((len(re.findall(r"^기록 \d+ · ", p, re.M))
                                         for p in decide_prompts), default=0),
        "decisions": decisions,
        "team_pattern": "팀이 닫은 수정과 비슷함" in section,
        "fix_ready": bool(pushed),
        "imports_constants": any(line.startswith("from orders.constants import") for line in added),
        "local_constant": any(re.match(r"[A-Z][A-Z0-9_]*\s*(:[^=]+)?=", line) for line in added),
        "pushed_diff": diff,
        "changed_files": sorted({line[6:] for line in diff.splitlines()
                                 if line.startswith("+++ b/")}),
        "fix_calls": len(fix_rows), "fix_models": sorted({c.model_id or c.model for c in fix_rows}),
        "fix_attempts": [a.attempts for a in attempts],
        "references": [a.references_json for a in attempts],
        "calls": [{"stage": c.stage, "model": c.model_id or c.model, "ok": c.ok,
                   "in": c.input_tokens, "out": c.output_tokens} for c in rows if not c.cache_hit],
        "section_tail": section[section.find("수정 판단:"):][:1500],
    }  # fmt: skip


@pytest.mark.docker
@pytest.mark.parametrize(
    "name",
    [
        "01-adopted-constants-file",
        "02-rejected-pattern-in-decide",
        "05-planted-instruction-in-record",
    ],
)
def test_search_off_and_on(name: str, eval_repo: Path, shared_venvs,  # noqa: F811
                           tool_cache: Path, tmp_path: Path, monkeypatch) -> None:  # fmt: skip
    needs_model()
    needs_embedder()
    pytest.importorskip("chromadb")
    runs = [
        _run(name, arm, n, tmp_path, eval_repo, shared_venvs, tool_cache, monkeypatch)
        for n in range(int(os.environ.get(RUNS_ENV, "1"))) for arm in ARMS
    ]  # fmt: skip
    if os.environ.get(OUT_ENV):
        out = Path(os.environ[OUT_ENV])
        out.mkdir(parents=True, exist_ok=True)
        (out / f"compare-{name}.json").write_text(json.dumps(runs, ensure_ascii=False, indent=1))
    for r in runs:
        print({k: r[k] for k in ("arm", "run", "records_in_decide_prompt", "decisions",
                                 "team_pattern", "fix_ready", "imports_constants",
                                 "local_constant", "changed_files", "fix_calls",
                                 "fix_attempts")})  # fmt: skip
    by_arm = {r["arm"]: r for r in runs}
    assert by_arm["off"]["records_in_decide_prompt"] == 0  # 검색을 끄면 기록이 들어가지 않는다
