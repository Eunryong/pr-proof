"""평가 세트 실행의 결과와 기준선 비교 (DESIGN.md 13절 6단계, 7.8의 `eval_runs`). 외부 호출 없음.

평가 세트 한 번은 `tests/e2e`의 docker 테스트 전부다. 케이스 파일(`eval/<묶음>/<케이스>.yaml`)을
읽는 하네스의 테스트는 케이스 하나가 한 줄이고, 그 밖의 테스트(도구를 끈 평가, 비교 하네스, 케이스
파일 형식 확인)는 테스트 이름으로 한 줄이다. 도구를 끈 평가의 재현율·정밀도는 따로 둔다.
"""

import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Final, Literal

from app.worker import checker_authoring_pure, rule_reading_pure
from app.worker.fix import fix_decision_pure, model_fix_pure
from app.worker.repo_config_pure import ModelSettings
from app.worker.rules import judge_pure, pr_check_pure, tool_choice_pure

Outcome = Literal["passed", "failed", "skipped"]

# 케이스 파일을 읽는 하네스 → eval의 묶음 폴더
CASE_HARNESSES: Final[Mapping[str, str]] = {
    "tests.e2e.test_rules_cases": "rules",
    "tests.e2e.test_lint_cases": "lint",
    "tests.e2e.test_judge_cases": "judge",
    "tests.e2e.test_tool_cases": "tools",
    "tests.e2e.test_rag_cases": "rag",
}
_PARAM: Final = re.compile(r"^(?P<test>[^\[]+)\[(?P<param>.+)\]$")


@dataclass(frozen=True)
class CaseResult:
    case: str  # "rules/01-print-in-orders" 또는 "test_tools_off_eval::test_…"
    outcome: Outcome
    seconds: float = 0.0

    @property
    def is_case_file(self) -> bool:
        return "::" not in self.case


@dataclass(frozen=True)
class RunSummary:
    """`eval_runs` 한 줄의 내용. 비교와 표시는 이것만 본다."""

    cases: tuple[CaseResult, ...]
    tools_off: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    models: Mapping[str, str] = field(default_factory=dict)
    prompts: Mapping[str, str] = field(default_factory=dict)

    def outcomes(self) -> dict[str, Outcome]:
        return {c.case: c.outcome for c in self.cases}

    def count(self, outcome: Outcome) -> int:
        return sum(1 for c in self.cases if c.outcome == outcome)


def parse_junit(xml_text: str) -> tuple[CaseResult, ...]:
    """pytest `--junitxml`의 결과를 케이스별로 읽는다. 같은 케이스가 두 번 나오면 나쁜 쪽을 남긴다
    (실패 > 건너뜀 > 통과)."""
    root = ET.fromstring(xml_text)
    rank = {"passed": 0, "skipped": 1, "failed": 2}
    found: dict[str, CaseResult] = {}
    for node in root.iter("testcase"):
        module, name = node.get("classname", ""), node.get("name", "")
        case = _case_id(module, name)
        if node.find("failure") is not None or node.find("error") is not None:
            outcome: Outcome = "failed"
        elif node.find("skipped") is not None:
            outcome = "skipped"
        else:
            outcome = "passed"
        result = CaseResult(case, outcome, round(float(node.get("time", "0") or 0), 1))
        old = found.get(case)
        if old is None or rank[outcome] > rank[old.outcome]:
            found[case] = result
    return tuple(sorted(found.values(), key=lambda c: c.case))


def _case_id(module: str, name: str) -> str:
    match = _PARAM.match(name)
    folder = CASE_HARNESSES.get(module)
    if folder and match:
        return f"{folder}/{match['param']}"
    return f"{module.rsplit('.', 1)[-1]}::{name}"


def prompt_versions() -> dict[str, str]:
    """모델을 부르는 단계마다 지금 코드의 프롬프트 판. 프롬프트를 고치면 바뀐다(9.1)."""
    return {
        "rules": rule_reading_pure.PROMPT_VERSION,
        "tool_choice": tool_choice_pure.PROMPT_VERSION,
        "checker": checker_authoring_pure.PROMPT_VERSION,
        "checker_path": checker_authoring_pure.PATH_PROMPT_VERSION,
        "judge": judge_pure.PROMPT_VERSION,
        "pr_plan": pr_check_pure.PLAN_PROMPT_VERSION,
        "pr_judge": pr_check_pure.JUDGE_PROMPT_VERSION,
        "decide": fix_decision_pure.PROMPT_VERSION,
        "fix": model_fix_pure.PROMPT_VERSION,
    }


def default_models() -> dict[str, str]:
    """`.pipeline.yaml`에 `model`이 없는 저장소가 쓰는 단계별 모델(10절의 기본값)."""
    return ModelSettings().model_dump(exclude={"calls"})


@dataclass(frozen=True)
class Change:
    key: str
    before: str
    after: str


@dataclass(frozen=True)
class Comparison:
    newly_failed: tuple[str, ...]  # 기준선에서 통과했고 지금 실패
    newly_passed: tuple[str, ...]  # 기준선에서 실패·건너뜀이었고 지금 통과
    other_changes: tuple[Change, ...]  # 그 밖의 결과 변화 (건너뜀이 됨 등)
    added: tuple[str, ...]  # 기준선에 없던 케이스
    removed: tuple[str, ...]  # 지금 없는 케이스
    tools_off: tuple[Change, ...]  # 도구를 끈 평가의 "조건/방법 재현율·정밀도" 전부 (같아도)
    settings: tuple[Change, ...]  # 프롬프트 판과 모델이 바뀐 곳

    @property
    def regressed(self) -> bool:
        return bool(self.newly_failed or self.removed)

    @property
    def changed(self) -> bool:
        return bool(
            self.newly_failed or self.newly_passed or self.other_changes or self.added
            or self.removed or self.settings or any(c.before != c.after for c in self.tools_off)
        )  # fmt: skip


def compare(base: RunSummary, new: RunSummary) -> Comparison:
    before, after = base.outcomes(), new.outcomes()
    shared = sorted(before.keys() & after.keys())
    newly_failed = tuple(k for k in shared if before[k] == "passed" and after[k] == "failed")
    newly_passed = tuple(k for k in shared if before[k] != "passed" and after[k] == "passed")
    listed = set(newly_failed) | set(newly_passed)
    other = tuple(
        Change(k, before[k], after[k]) for k in shared if before[k] != after[k] and k not in listed
    )
    return Comparison(
        newly_failed=newly_failed,
        newly_passed=newly_passed,
        other_changes=other,
        added=tuple(sorted(after.keys() - before.keys())),
        removed=tuple(sorted(before.keys() - after.keys())),
        tools_off=_metrics(base.tools_off, new.tools_off),
        settings=_changes("prompt", base.prompts, new.prompts)
        + _changes("model", base.models, new.models),
    )


def _metrics(
    before: Mapping[str, Mapping[str, str]], after: Mapping[str, Mapping[str, str]]
) -> tuple[Change, ...]:
    return tuple(
        Change(f"{key} {metric}", (before.get(key) or {}).get(metric, "-"),
               (after.get(key) or {}).get(metric, "-"))
        for key in sorted(before.keys() | after.keys())
        for metric in ("recall", "precision")
    )  # fmt: skip


def _changes(kind: str, before: Mapping[str, str], after: Mapping[str, str]) -> tuple[Change, ...]:
    return tuple(
        Change(f"{kind}.{k}", before.get(k, "-"), after.get(k, "-"))
        for k in sorted(before.keys() | after.keys())
        if before.get(k) != after.get(k)
    )


def render_summary(summary: RunSummary) -> str:
    files = [c for c in summary.cases if c.is_case_file]
    lines = [
        f"평가 케이스 {len(files)}개 · 통과 {sum(c.outcome == 'passed' for c in files)}"
        f" · 실패 {sum(c.outcome == 'failed' for c in files)}"
        f" · 건너뜀 {sum(c.outcome == 'skipped' for c in files)}",
        f"그 밖의 평가 테스트 {len(summary.cases) - len(files)}개 · 실패 "
        f"{sum(c.outcome == 'failed' for c in summary.cases if not c.is_case_file)}",
    ]
    failed = [c.case for c in summary.cases if c.outcome == "failed"]
    if failed:
        lines.append("실패: " + ", ".join(failed))
    if summary.tools_off:
        lines.append("도구를 끈 평가 (조건/방법: 재현율 · 정밀도):")
        lines += [f"  {k}: {v.get('recall', '-')} · {v.get('precision', '-')}"
                  for k, v in sorted(summary.tools_off.items())]  # fmt: skip
    else:
        lines.append("도구를 끈 평가: 결과 없음")
    return "\n".join(lines)


def render_comparison(c: Comparison, base_id: int) -> str:
    lines = [f"기준선 #{base_id}과 비교:"]

    def block(title: str, items: tuple[str, ...]) -> None:
        if items:
            lines.append(f"{title} {len(items)}개: " + ", ".join(items))

    block("새로 실패", c.newly_failed)
    block("새로 통과", c.newly_passed)
    block("새 케이스", c.added)
    block("없어진 케이스", c.removed)
    for title, changes in (("결과가 바뀐 케이스", c.other_changes),
                           ("바뀐 프롬프트·모델", c.settings)):  # fmt: skip
        if changes:
            lines.append(f"{title}:")
            lines += [f"  {ch.key}: {ch.before} → {ch.after}" for ch in changes]
    if c.tools_off:
        lines.append("도구를 끈 평가 (기준선 → 지금):")
        lines += [f"  {ch.key}: {ch.before} → {ch.after}" + (" (바뀜)" if ch.before != ch.after
                                                           else "")
                  for ch in c.tools_off]  # fmt: skip
    else:
        lines.append("도구를 끈 평가: 두 실행 모두 결과 없음")
    if not c.changed:
        lines.append("케이스 결과, 프롬프트·모델, 도구를 끈 평가가 모두 기준선과 같음")
    return "\n".join(lines)


def cases_json(cases: tuple[CaseResult, ...]) -> str:
    return json.dumps([asdict(c) for c in cases], ensure_ascii=False)


def cases_from_json(text: str) -> tuple[CaseResult, ...]:
    return tuple(CaseResult(**row) for row in json.loads(text))
