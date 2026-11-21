"""도구를 끈 평가 (docs/DESIGN.md 13절 "도구를 끈 평가", 완료 기준 1-16, 3-7, 3-8, 4-8).

eval/tools_off.yaml의 표본을 같은 규칙 읽기 결과로 세 조건에서 돌린다.

- 모델 판단만(`judge`): 개념 규칙 모두를 검사기 없이 모델 판단(`app/worker/rules/judge.py`)으로
  본다. 규칙이 스스로 모델 판단까지 내려가지 않아도 그 확인 방법 하나의 점수를 같은 표본에서 낸다
  (4-8).
- 도구 끔(`off`): 도구 목록을 내밀지 않는다. 검사기를 작성하고, 끝내 안 된 규칙은 운영과 같이 모델
  판단으로 내려간다(`doc_rules.author` → `find` → `judge`).
- 도구 켬(`on`): base 설정의 ruff를 넘긴다. 3단계 이후 운영 그대로다(도구 선택 → 검사기 → 모델
  판단).

같은 저장소(DB)를 쓰므로 뒤 조건은 앞 조건이 저장한 답을 다시 쓴다: 도구를 고르지 않은 규칙은 도구
끔의 검사기를, 모델 판단으로 내려간 규칙은 모델 판단만 조건의 답을 쓴다. 그래서 조건 사이의 차이는
확인 방법을 고른 결과 하나다.

정답은 개념마다 하나다. ruff 개념은 파이프라인의 샌드박스 ruff(`--isolated --select <코드>`)가
표본의 바뀐 줄에서 잡은 줄이다(`scope`가 있으면 그 안). 판단 케이스(`judge_cases`)는 케이스 파일의
`expect.violations`(케이스를 쓴 사람이 표시한 줄)다. 조건의 결과는 원문에 개념의 `needle`이 든 문서
규칙들이 잡은 줄의 합이고, 줄마다 그 규칙이 쓴 확인 방법(`tool`, `checker`, `judge`)으로 나눠 센다.
재현율 = 정답 중 잡은 비율, 정밀도 = 잡은 것 중 정답인 비율. 1단계 기준선의 정의와 같다.

기본은 가짜 모델(케이스의 `model`)로 한 번 돌아 하네스를 확인한다. `PR_PROOF_REAL_MODEL=1`이면
로그인된 claude를 파이프라인의 호출 경로 그대로 부른다(비용이 든다). 그때
`PR_PROOF_TOOLS_EVAL_RUNS`(기본 1)번을 매번 빈 DB로 돌고, `PR_PROOF_TOOLS_EVAL_OUT`이 있으면 결과
JSON을 그 폴더에 쓴다. `PR_PROOF_TOOLS_EVAL_MATHLLM`에 MathLLM 작업 폴더를 주면 그 파일을 복사한
base 전체(Python 파일의 모든 줄)를 표본으로 더한다. MathLLM은 파일이 많아 모델 판단만 조건을 돌리지
않는다.
"""

import json
import os
import shutil
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, Literal

import fakeredis
import pytest
import yaml

from app.worker import base_config, repo
from app.worker import rule_reading_pure as rule_reading
from app.worker.ai import claude_cli
from app.worker.ai.claude_cli import StructuredResult
from app.worker.budget import CallBudget
from app.worker.checker_pure import TOOL_KINDS
from app.worker.diff_pure import ChangedLines, added_lines
from app.worker.doc_rules_pure import Found, RuleChecked, RuleJudged, RuleOutcome, RulesRead
from app.worker.repo_config_pure import CallSettings, ModelSettings, RulesSettings
from app.worker.rule_pure import Rule, in_scope
from app.worker.rules import checkers, doc_rules
from app.worker.rules import judge as model_judge
from app.worker.rules import judge_pure as judge_model
from app.worker.rules import tool_choice_pure as tool_choice
from app.worker.sandbox import checker_runner
from app.worker.state import Store
from tests.sandbox_names import TEST_NAMES

from .test_lint_cases import _apply as apply_lint_edits
from .test_lint_cases import _git
from .test_rules_cases import REAL_MODEL_ENV, _apply, scripted_model

EVAL_DIR: Final = Path(__file__).resolve().parents[2] / "eval"
SPEC: Final = yaml.safe_load((EVAL_DIR / "tools_off.yaml").read_text())
RUNS_ENV: Final = "PR_PROOF_TOOLS_EVAL_RUNS"
OUT_ENV: Final = "PR_PROOF_TOOLS_EVAL_OUT"
MATHLLM_ENV: Final = "PR_PROOF_TOOLS_EVAL_MATHLLM"
# 실제 모델로 돌 때 저장소 하나의 한 회에서 넘지 않을 호출 수. 넘으면 측정을 멈춘다
MAX_CALLS: Final = 40
# 평가는 정확도를 잰다. PR 상한이 모델 판단을 중간에 멈추면 점수가 상한을 재게 되므로 넉넉히 둔다
EVAL_CALLS: Final = CallSettings(pr_base=150)
EMPTY_TREE: Final = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
Arm = Literal["judge", "off", "on"]
ARMS: Final[tuple[Arm, ...]] = ("judge", "off", "on")
Method = Literal["tool", "checker", "judge", "unchecked"]
Hit = tuple[str, str, int]  # (표본, 파일, 줄)


@dataclass(frozen=True)
class Concept:
    code: str  # ruff 코드, 또는 판단 케이스의 이름
    needle: str
    scope: tuple[str, ...] = ()
    marked: frozenset[Hit] | None = None  # 케이스가 표시한 정답. None이면 ruff가 정답이다


@dataclass(frozen=True)
class Sample:
    name: str
    root: Path
    diff: str
    changed: ChangedLines


@dataclass(frozen=True)
class Target:
    """한 저장소의 측정 대상: 규칙을 읽을 base와 표본들."""

    name: str
    base_root: Path
    base_sha: str
    samples: tuple[Sample, ...]
    concepts: tuple[Concept, ...]
    judge_alone: bool = True  # 모델 판단만 조건을 돌린다


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


def _sample(source: Path, dest: Path, case: dict, apply: Callable[..., None]) -> tuple[Sample, str]:
    """케이스의 base·PR 두 커밋짜리 저장소. 표본과 base 커밋을 돌려준다."""
    root = _new_repo(source, dest)
    apply(root, case.get("base"))
    base_sha = _commit(root, "base")
    apply(root, case["pr"])
    _commit(root, "pr")
    diff = _git(root, "diff", "--no-color", f"{base_sha}...HEAD")
    return Sample(dest.name, root, diff, added_lines(diff)), base_sha


def eval_target(eval_repo: Path, tmp: Path) -> Target:
    base_root = _new_repo(eval_repo, tmp / "eval-base")
    base_sha = _commit(base_root, "base")
    repo.record_git_dir(base_root)
    samples = []
    for rel in SPEC["cases"]:
        case = yaml.safe_load((EVAL_DIR / rel).read_text())
        # lint 케이스의 편집은 `prepend`를, 문서 규칙 케이스의 편집은 `create`를 쓴다
        apply = apply_lint_edits if rel.startswith("lint/") else _apply
        samples.append(_sample(eval_repo, tmp / Path(rel).stem, case, apply)[0])
    samples.append(_sample(eval_repo, tmp / "probe", {"pr": SPEC["probe"]}, _apply)[0])
    return Target("eval", base_root, base_sha, tuple(samples), _concepts("eval"))


def judge_target(eval_repo: Path, tmp: Path, entry: dict) -> Target:
    """판단 케이스 하나: 케이스의 base(README)에서 규칙을 읽고 PR 하나가 표본이다. 정답은 케이스의
    `expect.violations`다."""
    case = yaml.safe_load((EVAL_DIR / entry["case"]).read_text())
    name = Path(entry["case"]).stem
    sample, base_sha = _sample(eval_repo, tmp / name, case, _apply)
    repo.record_git_dir(sample.root)
    marked = frozenset(
        (sample.name, at.rsplit(":", 1)[0], int(at.rsplit(":", 1)[1]))
        for at in case["expect"]["violations"]
    )
    concept = Concept(name, entry["needle"], marked=marked)
    return Target(name, sample.root, base_sha, (sample,), (concept,))


def mathllm_target(source: Path, tmp: Path) -> Target:
    """base 전체가 표본이다: Python 파일의 모든 줄을 바뀐 줄로 본다 (1단계 기준선과 같음)."""
    root = _new_repo(source, tmp / "mathllm")
    base_sha = _commit(root, "base")
    repo.record_git_dir(root)
    diff = _git(root, "diff", "--no-color", EMPTY_TREE, "HEAD", "--", "*.py")
    sample = Sample("base", root, diff, added_lines(diff))
    return Target("mathllm", root, base_sha, (sample,), _concepts("mathllm"), judge_alone=False)


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


def _truth(target: Target, concept: Concept, sandbox: checkers.Sandbox) -> frozenset[Hit]:
    return concept.marked if concept.marked is not None else ruff_truth(target, concept, sandbox)


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
            else "tool_choice" if schema is tool_choice.ANSWER_SCHEMA
            else "judge" if schema is judge_model.ANSWER_SCHEMA else "checker"
        )  # fmt: skip
        result = self.ask(prompt, **kwargs)
        self.log.append({"arm": self.arm, "stage": stage, "model": kwargs.get("model"),
                         "model_id": result.model_id, "ok": result.ok,
                         "in": result.input_tokens, "out": result.output_tokens,
                         "cost": result.cost_usd})  # fmt: skip
        return result


def _method(outcome: RuleOutcome) -> tuple[Method, str]:
    """규칙이 쓴 확인 방법과 보일 글."""
    if isinstance(outcome, RuleJudged):
        return "judge", f"judge:{outcome.route} ({outcome.reason[:120]})"
    if not isinstance(outcome, RuleChecked):
        return "unchecked", f"unchecked:{outcome.kind}: {outcome.detail[:200]}"
    if outcome.checker.kind in TOOL_KINDS:
        return "tool", f"{outcome.checker.kind} {','.join(outcome.checker.codes)}"
    return "checker", f"checker {outcome.checker.kind}"


def _line(target: Target, hit: Hit) -> str:
    root = next(s.root for s in target.samples if s.name == hit[0])
    return (root / hit[1]).read_text().splitlines()[hit[2] - 1].strip()[:120]


@dataclass(frozen=True)
class ArmResult:
    methods: dict[str, tuple[Method, str]]  # 규칙 id → 확인 방법
    found: frozenset[tuple[str, Hit]]  # (규칙 id, 줄)
    rules: tuple[Rule, ...]


def _judge_alone(wanted: Sequence[Rule], target: Target, rt: doc_rules.Runtime) -> ArmResult:
    found: set[tuple[str, Hit]] = set()
    for sample in target.samples:
        got = model_judge.judge(wanted, sample.root, sample.diff, store=rt.store,
                                budget=rt.budget, model=rt.models.judge, ask=rt.ask)  # fmt: skip
        found |= {(v.rule_id, (sample.name, v.file, v.line)) for v in got.violations}
    alone: tuple[Method, str] = ("judge", "judge (모델 판단만)")
    return ArmResult({r.id: alone for r in wanted}, frozenset(found), tuple(wanted))


def _as_pipeline(read: RulesRead, target: Target, rt: doc_rules.Runtime) -> ArmResult:
    """운영의 순서 그대로: 검사 방법 정하기 → 도구·검사기 → 모델 판단."""
    outcomes = doc_rules.author(read, rt)
    assert isinstance(outcomes, tuple), outcomes
    found: set[tuple[str, Hit]] = set()
    for sample in target.samples:
        diff = sample.diff.encode()
        got = doc_rules.find(read, outcomes, sample.root, diff, rt, since_call=0)
        assert isinstance(got, Found), got
        got = doc_rules.judge(got, sample.root, diff, rt)
        found |= {(v.rule_id, (sample.name, v.file, v.line)) for v in got.violations}
    methods = {o.rule.id: _method(o) for o in outcomes}
    return ArmResult(methods, frozenset(found), read.rules)


def run_once(
    target: Target, ask: Callable[..., StructuredResult], data: Path, tool_cache: Path
) -> dict:
    """빈 DB로 규칙을 한 번 읽고 조건들을 돌린다. 개념·조건·확인 방법마다 점수와 근거를 돌려준다."""
    store = Store(data / "pipeline.db", fakeredis.FakeStrictRedis())
    budget = CallBudget(store, f"local/{target.name}", 1, target.base_sha, EVAL_CALLS)
    models = ModelSettings()
    calls = Calls(ask)
    ruff = base_config.load(target.base_root, target.base_sha, 2.0).ruff
    sandboxes: dict[Arm, checkers.Sandbox] = {
        "judge": checkers.Sandbox(tool_cache, image=TEST_NAMES.image),
        "off": checkers.Sandbox(tool_cache, image=TEST_NAMES.image),
        "on": checkers.Sandbox(tool_cache, ruff, image=TEST_NAMES.image),
    }
    runtimes = {arm: doc_rules.Runtime(store=store, budget=budget, models=models, ask=calls,
                                       sandbox=sandboxes[arm], ruff=ruff if arm == "on" else None)
                for arm in ARMS}  # fmt: skip
    read = doc_rules.read(target.base_root, target.base_sha, RulesSettings(), runtimes["off"])
    assert isinstance(read, RulesRead), read
    rules_read = len(read.rules)
    wanted = tuple(r for r in read.rules if any(c.needle in r.text for c in target.concepts))
    read = replace(read, rules=wanted)
    started = time.monotonic()
    arms: dict[Arm, ArmResult] = {}
    for arm in ARMS:
        if arm == "judge" and not target.judge_alone:
            continue
        calls.arm = arm
        rt = runtimes[arm]
        arms[arm] = (
            _judge_alone(wanted, target, rt) if arm == "judge" else _as_pipeline(read, target, rt)
        )

    def found_by(result: ArmResult, ids: set[str], truth: frozenset[Hit]) -> Score:
        return Score(truth, frozenset(h for rid, h in result.found if rid in ids))

    scores = []
    for concept in target.concepts:
        truth = _truth(target, concept, sandboxes["off"])
        for arm, result in arms.items():
            rules = [r for r in result.rules if concept.needle in r.text]
            score = found_by(result, {r.id for r in rules}, truth)
            by_method = {
                method: found_by(
                    result, {r.id for r in rules if result.methods[r.id][0] == method}, truth
                )
                for method in sorted({result.methods[r.id][0] for r in rules})
            }
            scores.append({
                "concept": concept.code, "truth_from": "ruff" if concept.marked is None else "case",
                "arm": arm, "score": score, "by_method": by_method,
                "rules": [{"id": r.id, "text": r.text, "scope": list(r.scope),
                           "exclude": list(r.exclude), "method": result.methods[r.id][1]}
                          for r in rules],
                "missed": [[*h, _line(target, h)] for h in sorted(truth - score.found)],
                "extra": [[*h, _line(target, h)] for h in sorted(score.found - truth)],
            })  # fmt: skip
    choices = {}
    for rule in wanted:
        entry = store.saved_checker(tool_choice.choice_key(rule, models.checker, ruff.version))
        if entry is not None:
            choices[rule.id] = {"accepted": entry.accepted, "detail": entry.detail,
                                "answer": json.loads(entry.attempts_json)}  # fmt: skip
    return {"target": target.name, "rules_read": rules_read, "wanted": len(wanted),
            "scores": scores, "tool_choices": choices, "calls": calls.log,
            "seconds": round(time.monotonic() - started)}  # fmt: skip


def _report(runs: Sequence[dict]) -> list[dict]:
    """조건·확인 방법마다 한 줄. 방법이 `all`인 줄은 그 개념의 규칙 전부다."""
    rows = []
    for run in runs:
        for s in run["scores"]:
            parts = {"all": s["score"], **s["by_method"]}
            for method, score in parts.items():
                rows.append({"run": run["rep"], "target": run["target"], "concept": s["concept"],
                             "truth_from": s["truth_from"], "arm": s["arm"], "method": method,
                             "recall": score.recall, "precision": score.precision})  # fmt: skip
    return rows


def by_method(runs: Sequence[dict]) -> dict[str, dict[str, str]]:
    """조건·확인 방법마다 모든 회·개념을 합친 재현율과 정밀도. 분모는 그 방법을 쓴 규칙이 있던
    (회, 개념)의 정답과, 그 방법이 잡은 줄이다."""
    totals: dict[str, list[int]] = {}
    for run in runs:
        for s in run["scores"]:
            for method, score in s["by_method"].items():
                t = totals.setdefault(f"{s['arm']}/{method}", [0, 0, 0])
                t[0] += len(score.truth & score.found)
                t[1] += len(score.truth)
                t[2] += len(score.found)
    return {key: {"recall": f"{hit}/{truth}", "precision": f"{hit}/{found}"}
            for key, (hit, truth, found) in sorted(totals.items())}  # fmt: skip


def judged_rules(runs: Sequence[dict]) -> dict[str, str]:
    """도구 끔·켬에서 운영 흐름이 모델 판단으로 보낸 규칙 수 / 규칙 수, 그리고 까닭별 수 (4.4)."""
    out: dict[str, str] = {}
    for arm in ("off", "on"):
        methods = [rule["method"] for run in runs for s in run["scores"] if s["arm"] == arm
                   for rule in s["rules"]]  # fmt: skip
        routes = Counter(m.split(" ", 1)[0] for m in methods if m.startswith("judge:"))
        detail = ", ".join(f"{k} {v}" for k, v in sorted(routes.items()))
        out[arm] = f"{sum(routes.values())}/{len(methods)}" + (f" ({detail})" if detail else "")
    return out


def _jsonable(runs: Sequence[dict]) -> list[dict]:
    def plain(score: Score) -> dict[str, str]:
        return {"recall": score.recall, "precision": score.precision}

    out = []
    for run in runs:
        scores = [{**s, "score": plain(s["score"]),
                   "by_method": {m: plain(p) for m, p in s["by_method"].items()}}
                  for s in run["scores"]]  # fmt: skip
        out.append({**run, "scores": scores})
    return out


@pytest.mark.docker
def test_tools_on_and_off_score_the_same_samples(
    eval_repo: Path, sandbox_image: str, tool_cache: Path, tmp_path: Path
) -> None:
    real = os.environ.get(REAL_MODEL_ENV) == "1"
    targets = [eval_target(eval_repo, tmp_path / "eval")]
    targets += [judge_target(eval_repo, tmp_path / "judge", e) for e in SPEC["judge_cases"]]
    if real and os.environ.get(MATHLLM_ENV):
        targets.append(mathllm_target(Path(os.environ[MATHLLM_ENV]), tmp_path / "mathllm"))
    runs_n = int(os.environ.get(RUNS_ENV, "1")) if real else 1
    runs = []
    for n in range(runs_n):
        for target in targets:
            ask = claude_cli.run_structured if real else _scripted(target)
            data = tmp_path / f"data-{target.name}-{n}"
            data.mkdir()
            runs.append({**run_once(target, ask, data, tool_cache), "rep": n + 1})
    if os.environ.get(OUT_ENV):
        out = Path(os.environ[OUT_ENV])
        out.mkdir(parents=True, exist_ok=True)
        report = {"rows": _report(runs), "by_method": by_method(runs),
                  "judged_rules": judged_rules(runs), "runs": _jsonable(runs)}  # fmt: skip
        (out / "tools_off.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
    for row in _report(runs):
        print(row)
    for run in runs:
        print(run["rep"], run["target"], Counter((c["arm"], c["stage"]) for c in run["calls"]))
    print(by_method(runs))
    print(judged_rules(runs))
    if real:
        return

    by = {(r["target"], s["concept"], s["arm"]): s for r in runs for s in r["scores"]}
    eval_calls = next(r["calls"] for r in runs if r["target"] == "eval")
    assert all(c["arm"] == "on" for c in eval_calls if c["stage"] == "tool_choice")
    assert {c["arm"] for c in eval_calls if c["stage"] == "judge"} == {"judge"}

    def at(concept: str, arm: Arm, target: str = "eval") -> dict:
        return by[(target, concept, arm)]

    assert at("T201", "on")["rules"][0]["method"] == "ruff T201"
    assert at("T201", "off")["rules"][0]["method"] == "checker ast-grep"
    assert at("T201", "on")["score"].recall == at("T201", "on")["score"].precision == "1/1"
    for code in ("PLR2004", "PLW0603"):
        # 도구를 고르지 않은 규칙은 두 조건이 같은 검사기를 쓴다
        assert at(code, "on")["score"] == at(code, "off")["score"]
        assert at(code, "on")["rules"][0]["method"] == "checker ast-grep"
    # README가 예외로 둔 `== 100`은 ruff만 잡는다: 정의대로 재현율에서 빠진다
    assert [m[:3] for m in at("PLR2004", "off")["missed"]] == [
        ["probe", "orders/services/probe.py", 14]
    ]
    assert at("PLW0603", "off")["score"].recall == "1/1"
    # 모델 판단만 조건: 가짜 답의 맞은 줄과 정답에 없는 줄이 그 방법의 점수로 나뉜다
    assert set(at("PLW0603", "judge")["by_method"]) == {"judge"}
    assert at("PLR2004", "judge")["score"].recall == "1/7"
    assert at("PLW0603", "judge")["score"].precision == "1/2"
    assert [m[:3] for m in at("PLW0603", "judge")["extra"]] == [
        ["probe", "orders/services/probe.py", 19]
    ]
    # 판단 케이스: 도구 끔·켬 모두 운영 흐름이 모델 판단으로 보낸다. 01은 가짜 검사기가 예시 확인을
    # 두 번 통과하지 못해서, 06(주석)은 검사기 작성 모델이 검사기로 가릴 수 없다고(none) 답해서다
    routes = {"01-verb-function-name": "judge:examples", "06-comment-matches-code": "judge:none"}
    for entry in SPEC["judge_cases"]:
        name = Path(entry["case"]).stem
        for arm in ARMS:
            got = at(name, arm, target=name)
            assert got["truth_from"] == "case"
            assert set(got["by_method"]) == {"judge"}, got["rules"]
            assert got["score"].recall == got["score"].precision == "1/1", got
            if arm != "judge":
                assert got["rules"][0]["method"].startswith(routes[name] + " ("), got["rules"]
    assert judged_rules(runs) == {"off": "2/5 (judge:examples 1, judge:none 1)",
                                  "on": "2/5 (judge:examples 1, judge:none 1)"}  # fmt: skip
    totals = by_method(runs)
    assert totals["off/checker"]["recall"] == "8/9"
    assert totals["off/judge"] == {"recall": "2/2", "precision": "2/2"}


def _scripted(target: Target) -> Callable[..., StructuredResult]:
    if target.name == "eval":
        return scripted_model(SPEC)
    (entry,) = [e for e in SPEC["judge_cases"] if Path(e["case"]).stem == target.name]
    return scripted_model(yaml.safe_load((EVAL_DIR / entry["case"]).read_text()))
