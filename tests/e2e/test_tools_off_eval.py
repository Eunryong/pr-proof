"""도구를 끈 평가 (docs/DESIGN.md 13절 "도구를 끈 평가", 완료 기준 1-16, 3-7, 3-8).

eval/tools_off.yaml의 표본을 같은 규칙 읽기 결과로 두 조건에서 돌린다.

- 도구 끔(`off`): 도구 목록을 내밀지 않는다(`plan_and_author(ruff=None)`). 1단계와 같이 검사기
  작성만 한다.
- 도구 켬(`on`): base 설정의 ruff(`base_config.load`)를 넘긴다. 3단계 운영과 같이 도구 선택이
  문서 규칙을 ruff 코드로 이을 수 있고, 잇지 못한 규칙은 검사기 작성으로 간다. 같은 저장소(DB)를
  쓰므로 그 검사기는 도구 끔에서 쓴 것을 다시 쓴다. 두 조건의 차이는 도구 선택 하나다.

정답은 파이프라인의 샌드박스 ruff(`--isolated --select <코드>`)가 표본의 바뀐 줄에서 잡은 줄이다
(개념에 `scope`가 있으면 그 안). 조건의 결과는 원문에 개념의 `needle`이 든 문서 규칙들이 잡은 줄의
합이다. 재현율 = 정답 중 잡은 비율, 정밀도 = 잡은 것 중 정답인 비율. 1단계 기준선의 정의와 같다.

기본은 가짜 모델(케이스의 `model`)로 한 번 돌아 하네스를 확인한다. `PR_PROOF_REAL_MODEL=1`이면
로그인된 claude를 파이프라인의 호출 경로 그대로 부른다(비용이 든다). 그때
`PR_PROOF_TOOLS_EVAL_RUNS`(기본 1)번을 매번 빈 DB로 돌고, `PR_PROOF_TOOLS_EVAL_OUT`이 있으면 결과
JSON을 그 폴더에 쓴다. `PR_PROOF_TOOLS_EVAL_MATHLLM`에 MathLLM 작업 폴더를 주면 그 파일을 복사한
base 전체(Python 파일의 모든 줄)를 표본으로 더한다.
"""

import json
import os
import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

import fakeredis
import pytest
import yaml

from app.models import rule_reading, tool_choice
from app.models.checker import TOOL_KINDS, tool_codes
from app.models.checker_authoring import Checked, RuleCheck
from app.models.diff import ChangedLines, added_lines
from app.models.doc_rules import RulesRead
from app.models.repo_config import CallSettings, ModelSettings, RulesSettings
from app.models.rule import in_scope
from app.sandbox import checker_runner
from app.worker import base_config, checkers, detect, doc_rules, repo
from app.worker.ai import claude_cli
from app.worker.ai.claude_cli import StructuredResult
from app.worker.budget import CallBudget
from app.worker.state import Store
from tests.sandbox_names import TEST_NAMES

from .test_lint_cases import _apply as apply_lint_edits
from .test_lint_cases import _git
from .test_rules_cases import NO_TOOL, REAL_MODEL_ENV, _answer, _apply

EVAL_DIR: Final = Path(__file__).resolve().parents[2] / "eval"
SPEC: Final = yaml.safe_load((EVAL_DIR / "tools_off.yaml").read_text())
RUNS_ENV: Final = "PR_PROOF_TOOLS_EVAL_RUNS"
OUT_ENV: Final = "PR_PROOF_TOOLS_EVAL_OUT"
MATHLLM_ENV: Final = "PR_PROOF_TOOLS_EVAL_MATHLLM"
# 실제 모델로 돌 때 저장소 하나의 한 회에서 넘지 않을 호출 수. 넘으면 측정을 멈춘다
MAX_CALLS: Final = 30
Arm = Literal["off", "on"]
ARMS: Final[tuple[Arm, ...]] = ("off", "on")
Hit = tuple[str, str, int]  # (표본, 파일, 줄)


@dataclass(frozen=True)
class Concept:
    code: str
    needle: str
    scope: tuple[str, ...] = ()


@dataclass(frozen=True)
class Sample:
    name: str
    root: Path
    changed: ChangedLines


@dataclass(frozen=True)
class Target:
    """한 저장소의 측정 대상: 규칙을 읽을 base와 표본들."""

    name: str
    base_root: Path
    base_sha: str
    samples: tuple[Sample, ...]
    concepts: tuple[Concept, ...]


@dataclass(frozen=True)
class Score:
    truth: frozenset[Hit]
    found: frozenset[Hit]

    @property
    def recall(self) -> str:
        return f"{len(self.truth & self.found)}/{len(self.truth)}"

    @property
    def precision(self) -> str:
        return f"{len(self.truth & self.found)}/{len(self.found)}"


def _concepts(repo_name: str) -> tuple[Concept, ...]:
    return tuple(
        Concept(c["code"], c["needle"], tuple(c.get("scope", ())))
        for c in SPEC["concepts"][repo_name]
    )


def _commit(path: Path, message: str) -> str:
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", message)
    return _git(path, "rev-parse", "HEAD")


def _new_repo(source: Path, dest: Path) -> Path:
    shutil.copytree(source, dest, ignore=shutil.ignore_patterns(".git"))
    _git(dest, "init", "-q", "-b", "main")
    return dest


def _sample(source: Path, dest: Path, case: dict, apply: Callable[..., None]) -> Sample:
    root = _new_repo(source, dest)
    apply(root, case.get("base"))
    base_sha = _commit(root, "base")
    apply(root, case["pr"])
    _commit(root, "pr")
    return Sample(dest.name, root, added_lines(_git(root, "diff", f"{base_sha}...HEAD")))


def eval_target(eval_repo: Path, tmp: Path) -> Target:
    base_root = _new_repo(eval_repo, tmp / "eval-base")
    base_sha = _commit(base_root, "base")
    repo.record_git_dir(base_root)
    samples = []
    for rel in SPEC["cases"]:
        case = yaml.safe_load((EVAL_DIR / rel).read_text())
        # lint 케이스의 편집은 `prepend`를, 문서 규칙 케이스의 편집은 `create`를 쓴다
        apply = apply_lint_edits if rel.startswith("lint/") else _apply
        samples.append(_sample(eval_repo, tmp / Path(rel).stem, case, apply))
    samples.append(_sample(eval_repo, tmp / "probe", {"pr": SPEC["probe"]}, _apply))
    return Target("eval", base_root, base_sha, tuple(samples), _concepts("eval"))


def mathllm_target(source: Path, tmp: Path) -> Target:
    """base 전체가 표본이다: Python 파일의 모든 줄을 바뀐 줄로 본다 (1단계 기준선과 같음)."""
    root = _new_repo(source, tmp / "mathllm")
    base_sha = _commit(root, "base")
    repo.record_git_dir(root)
    every = {
        rel: frozenset(range(1, len((root / rel).read_text().splitlines()) + 1))
        for rel in _git(root, "ls-files", "*.py").split()
    }
    return Target("mathllm", root, base_sha, (Sample("base", root, every),), _concepts("mathllm"))


def ruff_truth(target: Target, concept: Concept, sandbox: checkers.Sandbox) -> frozenset[Hit]:
    hits: set[Hit] = set()
    body = json.dumps({"codes": [concept.code]})
    for sample in target.samples:
        files = [f for f in sorted(sample.changed)
                 if f.endswith(".py") and in_scope(concept.scope, f)]  # fmt: skip
        if not files:
            continue
        run = sandbox.on_files("ruff", body, sample.root, files)
        assert isinstance(run, checker_runner.Ran), run
        hits |= {(sample.name, m.file, m.line) for m in run.matches
                 if m.line in sample.changed.get(m.file, ())}  # fmt: skip
    return frozenset(hits)


class Calls:
    """모델 호출을 단계·조건별로 센다. 넘기는 인자는 바꾸지 않는다."""

    def __init__(self, ask: Callable[..., StructuredResult]) -> None:
        self.ask = ask
        self.arm = "read"
        self.log: list[dict] = []

    def __call__(self, prompt: str, **kwargs) -> StructuredResult:
        if len(self.log) >= MAX_CALLS:
            raise RuntimeError(f"모델 호출이 {MAX_CALLS}회를 넘으려 함")
        schema = kwargs.get("schema")
        stage = (
            "rules" if schema is rule_reading.ANSWER_SCHEMA
            else "tool_choice" if schema is tool_choice.ANSWER_SCHEMA else "checker"
        )  # fmt: skip
        result = self.ask(prompt, **kwargs)
        self.log.append({"arm": self.arm, "stage": stage, "model": kwargs.get("model"),
                         "model_id": result.model_id, "ok": result.ok,
                         "in": result.input_tokens, "out": result.output_tokens,
                         "cost": result.cost_usd})  # fmt: skip
        return result


def scripted_ask(spec: dict) -> Callable[..., StructuredResult]:
    rules = spec["model"]["rules"]
    reading = {"rules": [
        {"file": "README.md", "section": r["section"], "quote": r["quote"],
         "scope": r.get("scope", []), "scope_exclude": [], "scope_reason": "", "examples": [],
         "check": "checker", "reason": "", "fix": "model"}
        for r in rules
    ]}  # fmt: skip

    def ask(prompt: str, *, schema, **kw) -> StructuredResult:
        if schema is rule_reading.ANSWER_SCHEMA:
            return _answer(reading)
        (rule,) = [r for r in rules if r["quote"] in prompt]
        if schema is tool_choice.ANSWER_SCHEMA:
            return _answer(rule.get("tool") or NO_TOOL)
        return _answer({"checker": rule["checker"]["body"],
                        "examples": rule["checker"]["examples"]})  # fmt: skip

    return ask


def _method(check: RuleCheck) -> str:
    if not isinstance(check, Checked):
        return f"unchecked:{check.kind}: {check.detail[:200]}"
    kind = check.checker.kind
    if kind in TOOL_KINDS:
        return f"{kind} {','.join(tool_codes(check.checker.body))}"
    return f"checker {kind}"


def _line(target: Target, hit: Hit) -> str:
    root = next(s.root for s in target.samples if s.name == hit[0])
    return (root / hit[1]).read_text().splitlines()[hit[2] - 1].strip()[:120]


def run_once(
    target: Target, ask: Callable[..., StructuredResult], data: Path, tool_cache: Path
) -> dict:
    """빈 DB로 규칙을 한 번 읽고 두 조건을 돌린다. 개념마다 조건별 점수와 근거를 돌려준다."""
    store = Store(data / "pipeline.db", fakeredis.FakeStrictRedis())
    budget = CallBudget(store, f"local/{target.name}", 1, target.base_sha, CallSettings())
    models = ModelSettings()
    calls = Calls(ask)
    ruff = base_config.load(target.base_root, target.base_sha, 2.0).ruff
    sandboxes: dict[Arm, checkers.Sandbox] = {
        "off": checkers.Sandbox(tool_cache, image=TEST_NAMES.image),
        "on": checkers.Sandbox(tool_cache, ruff, image=TEST_NAMES.image),
    }
    rt = doc_rules.Runtime(store=store, budget=budget, models=models, ask=calls,
                           sandbox=sandboxes["off"])  # fmt: skip
    read = doc_rules.read(target.base_root, target.base_sha, RulesSettings(), rt)
    assert isinstance(read, RulesRead), read
    wanted = [r for r in read.rules if any(c.needle in r.text for c in target.concepts)]
    started = time.monotonic()
    arms: dict[str, dict] = {}
    for arm in ARMS:
        calls.arm = arm
        checks = checkers.plan_and_author(
            wanted, ruleset_key=read.key, store=store, budget=budget, models=models, ask=calls,
            sandbox=sandboxes[arm], ruff=ruff if arm == "on" else None,
        )  # fmt: skip
        found: set[tuple[str, Hit]] = set()
        for sample in target.samples:
            got = detect.detect(checks, sample.root, sample.changed, sandbox=sandboxes[arm])
            found |= {(v.rule_id, (sample.name, v.file, v.line)) for v in got.violations}
        arms[arm] = {"checks": checks, "found": found}
    scores = []
    for concept in target.concepts:
        truth = ruff_truth(target, concept, sandboxes["off"])
        for arm in ARMS:
            rules = [c for c in arms[arm]["checks"] if concept.needle in c.rule.text]
            ids = {c.rule.id for c in rules}
            score = Score(truth, frozenset(h for rid, h in arms[arm]["found"] if rid in ids))
            scores.append({
                "concept": concept.code, "arm": arm, "score": score,
                "rules": [{"id": c.rule.id, "text": c.rule.text, "scope": list(c.rule.scope),
                           "exclude": list(c.rule.exclude), "method": _method(c)} for c in rules],
                "missed": [[*h, _line(target, h)] for h in sorted(truth - score.found)],
                "extra": [[*h, _line(target, h)] for h in sorted(score.found - truth)],
            })  # fmt: skip
    choices = {}
    for rule in wanted:
        entry = store.saved_checker(tool_choice.choice_key(rule, models.checker, ruff.version))
        if entry is not None:
            choices[rule.id] = {"accepted": entry.accepted, "detail": entry.detail,
                                "answer": json.loads(entry.attempts_json)}  # fmt: skip
    return {"target": target.name, "rules_read": len(read.rules), "wanted": len(wanted),
            "scores": scores, "tool_choices": choices, "calls": calls.log,
            "seconds": round(time.monotonic() - started)}  # fmt: skip


def _report(runs: Sequence[dict]) -> list[dict]:
    rows = []
    for n, run in enumerate(runs, 1):
        for s in run["scores"]:
            rows.append({"run": n, "target": run["target"], "concept": s["concept"],
                         "arm": s["arm"], "recall": s["score"].recall,
                         "precision": s["score"].precision,
                         "methods": [r["method"] for r in s["rules"]]})  # fmt: skip
    return rows


def _jsonable(runs: Sequence[dict]) -> list[dict]:
    out = []
    for run in runs:
        scores = [{**s, "score": {"recall": s["score"].recall,
                                  "precision": s["score"].precision}}
                  for s in run["scores"]]  # fmt: skip
        out.append({**run, "scores": scores})
    return out


@pytest.mark.docker
def test_tools_on_and_off_score_the_same_samples(
    eval_repo: Path, sandbox_image: str, tool_cache: Path, tmp_path: Path
) -> None:
    real = os.environ.get(REAL_MODEL_ENV) == "1"
    targets = [eval_target(eval_repo, tmp_path / "eval")]
    if real and os.environ.get(MATHLLM_ENV):
        targets.append(mathllm_target(Path(os.environ[MATHLLM_ENV]), tmp_path / "mathllm"))
    runs_n = int(os.environ.get(RUNS_ENV, "1")) if real else 1
    runs = []
    for n in range(runs_n):
        for target in targets:
            ask = claude_cli.run_structured if real else scripted_ask(SPEC)
            data = tmp_path / f"data-{target.name}-{n}"
            data.mkdir()
            runs.append(run_once(target, ask, data, tool_cache))
    if os.environ.get(OUT_ENV):
        out = Path(os.environ[OUT_ENV])
        out.mkdir(parents=True, exist_ok=True)
        (out / "tools_off.json").write_text(
            json.dumps(
                {"rows": _report(runs), "runs": _jsonable(runs)}, ensure_ascii=False, indent=1
            )
        )
    for row in _report(runs):
        print(row)
    if real:
        return

    (run,) = runs
    by = {(s["concept"], s["arm"]): s for s in run["scores"]}
    assert all(c["arm"] == "on" for c in run["calls"] if c["stage"] == "tool_choice")
    assert by[("T201", "on")]["rules"][0]["method"] == "ruff T201"
    assert by[("T201", "off")]["rules"][0]["method"] == "checker ast-grep"
    assert by[("T201", "on")]["score"].recall == by[("T201", "on")]["score"].precision == "1/1"
    for code in ("PLR2004", "PLW0603"):
        # 도구를 고르지 않은 규칙은 두 조건이 같은 검사기를 쓴다
        assert by[(code, "on")]["score"] == by[(code, "off")]["score"]
        assert by[(code, "on")]["rules"][0]["method"] == "checker ast-grep"
    # README가 예외로 둔 `== 100`은 ruff만 잡는다: 정의대로 재현율에서 빠진다
    assert [m[:3] for m in by[("PLR2004", "off")]["missed"]] == [
        ["probe", "orders/services/probe.py", 14]
    ]
    assert by[("PLW0603", "off")]["score"].recall == "1/1"
