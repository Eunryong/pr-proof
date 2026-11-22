"""모델의 직접 판단 (docs/DESIGN.md 4.5): 검사기를 만들지 못한 규칙을 PR이 바꾼 hunk에 대해 모델이
본다.

모델은 도구 없이 부른다 (`run_structured`, 8.4). 호출은 파일 단위로 묶고(그 파일의 hunk들과 거기
적용되는 규칙들) 답은 (규칙, hunk) 쌍마다 `model_cache`에 저장한다. 저장된 쌍은 다시 묻지 않는다.
답은 결정적으로 확인해 PR이 바꾼 줄을 그 줄의 내용 그대로 인용한 것만 위반으로 남긴다.

호출은 PR 상한(9.2)에서 센다. 상한에 걸리거나 호출이 실패하면 남은 쌍을 그 규칙의 "판단하지
못함"으로 두고 저장하지 않는다. `/fix`나 다음 검사가 남은 쌍만 다시 묻는다. 모델이 이 자료로는
판단할 수 없다고 답한 쌍은 그 까닭과 함께 저장한다(같은 입력에는 같은 답이다).
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from app.worker.budget import CallBudget
from app.worker.call_budget_pure import CALL_BUDGET_USD, Denied, PrScope
from app.worker.diff_pure import added_lines
from app.worker.doc_rules_pure import JudgeGap
from app.worker.rule_pure import Rule, checked_file
from app.worker.rules.checkers import STALE
from app.worker.rules.detect import source_lines
from app.worker.rules.judge_pure import (
    ANSWER_SCHEMA,
    MAX_HUNKS_PER_CALL,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    Asked,
    Flag,
    Hunk,
    JudgeUsage,
    build_prompt,
    flags_from_json,
    flags_to_json,
    hunks_for,
    judge_key,
    parse_answer,
    undecided_from_json,
    undecided_to_json,
    verify_answer,
)
from app.worker.rules.rules import AskModel
from app.worker.state import Store
from app.worker.violation_pure import Violation, violation_id

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Gap:
    """한 규칙에서 판단하지 못한 쌍."""

    kind: JudgeGap
    detail: str
    pairs: int


@dataclass(frozen=True)
class Undecided:
    """한 규칙에서 모델이 이 자료로는 판단할 수 없다고 답한 쌍."""

    pairs: int
    why: str  # 처음 답한 까닭


@dataclass(frozen=True)
class Judged:
    violations: tuple[Violation, ...]
    gaps: Mapping[str, Gap]  # 규칙 id → 판단하지 못한 쌍. 다 판단한 규칙은 없다
    usage: JudgeUsage
    undecided: Mapping[str, Undecided] = field(default_factory=dict)
    pairs: Mapping[str, int] = field(default_factory=dict)  # 규칙 id → 본 (규칙, hunk) 쌍 수


@dataclass
class _Run:
    """판단 한 번의 누적 값."""

    flags: dict[tuple[str, str], list[Flag]] = field(default_factory=dict)  # (파일, 규칙) → 위반
    gaps: dict[str, Gap] = field(default_factory=dict)
    undecided: dict[str, Undecided] = field(default_factory=dict)
    pairs: dict[str, int] = field(default_factory=dict)
    stop: tuple[JudgeGap, str] | None = None  # 상한에 걸려 남은 쌍을 묻지 않는다
    calls: int = 0
    cached: int = 0
    dropped: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    models: list[str] = field(default_factory=list)

    def found(self) -> int:
        return sum(len(f) for f in self.flags.values())

    def undecide(self, rule_id: str, why: str) -> None:
        old = self.undecided.get(rule_id)
        self.undecided[rule_id] = Undecided((old.pairs if old else 0) + 1, old.why if old else why)

    def leave(self, asked: Asked, kind: JudgeGap, detail: str) -> None:
        for rule in asked.rules:
            gap = self.gaps.get(rule.id)
            self.gaps[rule.id] = Gap(kind, detail, (gap.pairs if gap else 0) + 1)


def judge(
    rules: Sequence[Rule],
    worktree: Path,
    changes_diff: str,
    *,
    store: Store,
    budget: CallBudget,
    model: str,
    ask: AskModel,
    known: int = 0,
    is_stale: Callable[[], bool] = lambda: False,
) -> Judged:
    """rules를 PR이 바꾼 파일의 hunk에 대해 판단한다. model은 `model.judge`다.

    known은 이 검사에서 도구·검사기가 이미 찾은 위반 수다. PR 상한의 대상 위반 수에 더한다 (9.2).
    새 커밋이 와서 낡았으면 남은 파일은 묻지 않는다 (결과는 쓰이지 않는다).
    """
    run = _Run()
    hunks_seen = 0
    for file, changed in sorted(added_lines(changes_diff).items()):
        file_rules = tuple(r for r in rules if checked_file(r, file))
        if not file_rules:
            continue
        lines = source_lines(worktree, file)
        if not lines:
            continue  # 읽을 수 없는 파일(링크, 특수 파일, 크기 초과). 검사기도 그 파일을 버린다
        hunks = hunks_for(file, lines, changed)
        hunks_seen += len(hunks)
        for rule in file_rules:
            run.pairs[rule.id] = run.pairs.get(rule.id, 0) + len(hunks)
        to_ask = _from_cache(file_rules, hunks, store=store, model=model, run=run)
        for start in range(0, len(to_ask), MAX_HUNKS_PER_CALL):
            chunk = to_ask[start : start + MAX_HUNKS_PER_CALL]
            if run.stop is None and is_stale():
                run.stop = ("cap", STALE)
            if run.stop is not None:
                for asked in chunk:
                    run.leave(asked, *run.stop)
                continue
            _ask(chunk, store=store, budget=budget.with_targets(known + run.found()), model=model,
                 ask=ask, run=run)  # fmt: skip
    if run.cached:
        budget.record_hit(PrScope(), stage="judge", tier="light", model=model)
    usage = JudgeUsage(
        rules=len(rules), hunks=hunks_seen, calls=run.calls, cached=run.cached,
        dropped=run.dropped, input_tokens=run.input_tokens, output_tokens=run.output_tokens,
        models=tuple(sorted(set(run.models))),
    )  # fmt: skip
    return Judged(_violations(run.flags, rules, worktree), dict(run.gaps), usage,
                  dict(run.undecided), dict(run.pairs))  # fmt: skip


def _from_cache(
    rules: Sequence[Rule], hunks: Sequence[Hunk], *, store: Store, model: str, run: _Run
) -> list[Asked]:
    """저장된 쌍은 답을 꺼내 run에 넣고, 저장되지 않은 쌍만 hunk마다 묶어 돌려준다."""
    to_ask = []
    for hunk in hunks:
        missing = []
        for rule in rules:
            entry = store.cached_result(judge_key(rule, hunk, model))
            if entry is None:
                missing.append(rule)
                continue
            run.cached += 1
            why = undecided_from_json(entry.result_json)
            if why is not None:
                run.undecide(rule.id, why)
                continue
            flags = flags_from_json(rule.id, hunk, entry.result_json)
            run.flags.setdefault((hunk.file, rule.id), []).extend(flags)
        if missing:
            to_ask.append(Asked(hunk, tuple(missing)))
    return to_ask


def _ask(
    chunk: Sequence[Asked], *, store: Store, budget: CallBudget, model: str, ask: AskModel,
    run: _Run,
) -> None:  # fmt: skip
    """한 파일의 hunk 묶음 하나를 묻는다."""
    call = budget.begin(PrScope(), stage="judge", tier="light", model=model)
    if isinstance(call, Denied):
        log.info("모델 판단을 하지 않음: %s", call.reason)
        run.stop = ("cap", call.reason)
        for asked in chunk:
            run.leave(asked, *run.stop)
        return
    answer = ask(
        build_prompt(chunk),
        system_prompt=SYSTEM_PROMPT,
        schema=ANSWER_SCHEMA,
        model=model,
        max_budget_usd=CALL_BUDGET_USD["light"],
    )
    failure = None if answer.ok else f"{answer.error}: {answer.detail}"
    parsed = None
    if failure is None:
        try:
            parsed = parse_answer(answer.data or {})
        except ValueError as e:
            failure = f"답의 형식이 틀림: {str(e).splitlines()[0]}"
    budget.finish(
        call, ok=failure is None, error=failure, model_id=answer.model_id,
        input_tokens=answer.input_tokens, output_tokens=answer.output_tokens,
        permission_denials=answer.permission_denials,
    )  # fmt: skip
    run.calls += 1
    run.input_tokens += answer.input_tokens or 0
    run.output_tokens += answer.output_tokens or 0
    run.models.append(answer.model_id or model)
    if failure is not None or parsed is None:
        log.warning("모델 판단 실패: %s", failure)
        for asked in chunk:
            run.leave(asked, "model_failed", failure or "")
        return
    verified = verify_answer(chunk, parsed)
    for reason in verified.dropped:
        log.info("모델 판단의 답을 버림: %s", reason)
    run.dropped += len(verified.dropped)
    for (at, rule_id), flags in verified.kept.items():
        hunk = chunk[at].hunk
        rule = next(r for r in chunk[at].rules if r.id == rule_id)
        why = verified.undecided.get((at, rule_id))
        saved = flags_to_json(hunk, flags) if why is None else undecided_to_json(why)
        store.save_result(
            judge_key(rule, hunk, model), stage="judge", model=answer.model_id or model,
            prompt_version=PROMPT_VERSION, result_json=saved,
        )  # fmt: skip
        if why is not None:
            run.undecide(rule_id, why)
        run.flags.setdefault((hunk.file, rule_id), []).extend(flags)


def _violations(
    flags: Mapping[tuple[str, str], Sequence[Flag]], rules: Sequence[Rule], worktree: Path
) -> tuple[Violation, ...]:
    """찾은 위반을 7.3의 위반으로. 같은 줄 내용의 몇 번째인지는 그 파일에서 그 줄 위에 같은 내용의
    줄이 몇 개인지로 센다 (줄 번호를 id에 쓰지 않는다)."""
    texts = {r.id: r.text for r in rules}
    out = []
    for (file, rule_id), found in flags.items():
        lines = [line.strip() for line in source_lines(worktree, file)]
        for flag in sorted({f.line: f for f in found}.values(), key=lambda f: f.line):
            occurrence = lines[: flag.line - 1].count(flag.quote)
            out.append(Violation(
                id=violation_id(rule_id, file, flag.quote, occurrence), rule_id=rule_id,
                file=file, line=flag.line, message=flag.why or texts[rule_id], found_by="model",
                line_text=flag.quote,
            ))  # fmt: skip
    return tuple(sorted(out, key=lambda v: (v.file, v.line, v.rule_id)))
