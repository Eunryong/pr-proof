"""모델의 직접 판단 (docs/DESIGN.md 4.5): 검사기를 만들지 못한 규칙을 PR이 바꾼 hunk에 대해 모델이
본다.

모델은 worktree의 일반 파일을 복사한 모델 폴더를 읽을 수 있게 하고 부른다(`ModelBackend.reading`,
claude 구현은 읽기 도구 Read, Grep, Glob만 준다, 8.4). hunk 밖을 봐야 판단할 수 있으면 모델이 그곳을
찾아 읽는다. 폴더는 판단 한 번에 하나를 열고(묻는 호출이 있을 때만) 모델이 무엇을 써도 worktree에
반영하지 않는다. 호출은 파일 단위로 묶고(그 파일의 hunk들과 거기 적용되는 규칙들) 답은 (규칙, hunk)
쌍마다 `model_cache`에 모델이 읽은 경로들의 해시와 함께 저장한다. 저장된 쌍은 읽은 경로가 모두
그대로일 때만 다시 쓴다. 답은 결정적으로 확인해 PR이 바꾼 줄을 그 줄의 내용 그대로 인용한 것만
위반으로 남긴다.

저장한 값에는 모델의 근거 메모와 답을 낸 PR·커밋도 둔다. 같은 PR의 저장된 쌍이 읽은 곳이 바뀌어
다시 물어야 하면 그 쌍의 지난 판단과 근거, 그 커밋과 지금 커밋 사이에 바뀐 파일(git)과 읽은 곳 중
해시가 달라진 경로를 같이 준다. 모델이 유지한다고 답하면 지난 답을 그대로 다시 쓴다(결정적 확인은
똑같이 지난다). 다른 PR이 저장한 같은 키의 답은 지금처럼 읽은 곳이 그대로일 때만 쓰고, 지난
판단으로 보이지는 않는다.

호출은 PR 상한(9.2)에서 센다. 상한에 걸리거나 호출이 실패하면 남은 쌍을 그 규칙의 "판단하지
못함"으로 두고 저장하지 않는다. `/fix`나 다음 검사가 남은 쌍만 다시 묻는다. 모델이 읽어 봐도
판단할 수 없다고 답한 쌍은 그 까닭과 함께 저장한다(같은 입력에는 같은 답이다).
"""

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final

from app.worker.core.diff_pure import added_lines
from app.worker.core.rule_pure import Rule, checked_file
from app.worker.core.violation_pure import Violation, violation_id
from app.worker.doc_rules_pure import JudgeGap
from app.worker.git import repo
from app.worker.model.claude import workdir
from app.worker.model.model_backend import AskModel, ReadCode, StructuredResult
from app.worker.rules.checkers import STALE
from app.worker.rules.detect import source_lines
from app.worker.rules.judge_pure import (
    ANSWER_SCHEMA,
    CALL_BUDGET_USD,
    MAX_HUNKS_PER_CALL,
    MAX_TOOL_USES,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    Asked,
    Flag,
    Hunk,
    JudgeUsage,
    Origin,
    Prior,
    build_prompt,
    flags_from_json,
    flags_to_json,
    hunks_for,
    judge_key,
    parse_answer,
    pr_label,
    prior_from_json,
    reads_from_json,
    undecided_from_json,
    undecided_to_json,
    verify_answer,
)
from app.worker.store.budget import CallBudget
from app.worker.store.call_budget_pure import Denied, PrScope
from app.worker.store.state import Store

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
    reasked: int = 0  # 지난 판단을 주고 다시 물은 쌍
    kept_prior: int = 0  # 그중 모델이 지난 판단을 유지한 쌍
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


class CodeReader:
    """판단 한 번이 보는 PR의 코드: 읽기 도구를 주는 모델 폴더(처음 물을 때 연다)와 경로의 해시,
    앞 커밋과 지금 커밋 사이에 바뀐 파일."""

    def __init__(self, worktree: Path, read_code: ReadCode, stack: ExitStack) -> None:
        self.worktree, self._read_code, self._stack = worktree, read_code, stack
        self._asker: AskModel | None = None
        self._files: list[str] | None = None
        self._digests: dict[str, str] = {}
        self._changed: dict[tuple[str, str], tuple[str, ...] | None] = {}

    def ask(self, prompt: str, **kwargs: object) -> StructuredResult:
        if self._asker is None:
            self._asker = self._stack.enter_context(self._read_code(self.worktree))
        return self._asker(prompt, **kwargs)

    def digests(self, paths: Sequence[str]) -> dict[str, str]:
        """경로(worktree 기준) → 지금의 해시 (`workdir.path_digest`). git이 실패하면 GitError."""
        if paths and self._files is None:
            self._files = repo.list_files(self.worktree)
        for path in paths:
            if path not in self._digests:
                self._digests[path] = workdir.path_digest(self.worktree, path, self._files or [])
        return {p: self._digests[p] for p in paths}

    def unchanged(self, reads: Mapping[str, str]) -> bool:
        """저장한 답을 낼 때 읽은 경로들이 모두 그대로인지. 확인하지 못하면 그대로가 아니다."""
        try:
            return self.digests(sorted(reads)) == dict(reads)
        except repo.GitError as e:
            log.warning("모델 판단이 읽은 파일을 확인하지 못함: %s", e)
            return False

    def moved(self, reads: Mapping[str, str]) -> tuple[str, ...]:
        """reads 중 지금 해시가 저장한 해시와 다른 경로. 확인하지 못하면 빈 튜플이다."""
        try:
            now = self.digests(sorted(reads))
        except repo.GitError:
            return ()
        return tuple(p for p in sorted(reads) if now[p] != reads[p])

    def changed_since(self, sha: str, head: str) -> tuple[str, ...] | None:
        """커밋 sha와 head 사이에 바뀐 파일(`repo.changed_paths`). 같은 PR의 앞 커밋이라 worktree의
        저장소(bare 캐시)에 있다. 커밋 해시가 아니거나 없어서(강제 push 뒤 지워짐 등) 구하지
        못하면 None이다."""
        if (sha, head) not in self._changed:
            out = None
            if _SHA.fullmatch(sha) and _SHA.fullmatch(head):
                try:
                    out = repo.changed_paths(self.worktree, sha, head)
                except repo.GitError as e:
                    log.info(
                        "지난 판단 뒤 바뀐 파일을 구하지 못함 %s..%s: %s", sha[:7], head[:7], e
                    )
            self._changed[(sha, head)] = out
        return self._changed[(sha, head)]


_SHA: Final = re.compile(r"[0-9a-f]{40}")


def judge(
    rules: Sequence[Rule],
    worktree: Path,
    changes_diff: str,
    *,
    store: Store,
    budget: CallBudget,
    model: str,
    read_code: ReadCode,
    known: int = 0,
    is_stale: Callable[[], bool] = lambda: False,
) -> Judged:
    """rules를 PR이 바꾼 파일의 hunk에 대해 판단한다. model은 `model.judge`다. read_code는
    worktree의 코드를 읽을 수 있게 하고 묻는 폴더를 연다(`ModelBackend.reading`).

    known은 이 검사에서 도구·검사기가 이미 찾은 위반 수다. PR 상한의 대상 위반 수에 더한다 (9.2).
    새 커밋이 와서 낡았으면 남은 파일은 묻지 않는다 (결과는 쓰이지 않는다).
    """
    with ExitStack() as stack:
        code = CodeReader(worktree, read_code, stack)
        return _judge(rules, worktree, changes_diff, store=store, budget=budget, model=model,
                      code=code, known=known, is_stale=is_stale)  # fmt: skip


def _judge(
    rules: Sequence[Rule],
    worktree: Path,
    changes_diff: str,
    *,
    store: Store,
    budget: CallBudget,
    model: str,
    code: CodeReader,
    known: int,
    is_stale: Callable[[], bool],
) -> Judged:
    run = _Run()
    hunks_seen = 0
    origin = Origin(pr_label(budget.repo, budget.pr_number), budget.head_sha)
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
        to_ask = _from_cache(file_rules, hunks, store=store, model=model, run=run, code=code,
                             origin=origin)  # fmt: skip
        for start in range(0, len(to_ask), MAX_HUNKS_PER_CALL):
            chunk = to_ask[start : start + MAX_HUNKS_PER_CALL]
            if run.stop is None and is_stale():
                run.stop = ("cap", STALE)
            if run.stop is not None:
                for asked in chunk:
                    run.leave(asked, *run.stop)
                continue
            _ask(chunk, store=store, budget=budget.with_targets(known + run.found()), model=model,
                 code=code, run=run, origin=origin)  # fmt: skip
    if run.cached:
        budget.record_hit(PrScope(), stage="judge", tier="light", model=model)
    if run.reasked:
        log.info("모델 판단: 지난 판단을 주고 다시 물음 %d쌍, 그중 유지 %d쌍", run.reasked,
                 run.kept_prior)  # fmt: skip
    usage = JudgeUsage(
        rules=len(rules), hunks=hunks_seen, calls=run.calls, cached=run.cached,
        dropped=run.dropped, input_tokens=run.input_tokens, output_tokens=run.output_tokens,
        models=tuple(sorted(set(run.models))), reasked=run.reasked, kept_prior=run.kept_prior,
    )  # fmt: skip
    return Judged(_violations(run.flags, rules, worktree), dict(run.gaps), usage,
                  dict(run.undecided), dict(run.pairs))  # fmt: skip


def _from_cache(
    rules: Sequence[Rule], hunks: Sequence[Hunk], *, store: Store, model: str, run: _Run,
    code: CodeReader, origin: Origin,
) -> list[Asked]:  # fmt: skip
    """저장된 쌍은 답을 꺼내 run에 넣고, 저장되지 않았거나 그 답을 낼 때 모델이 읽은 곳이 바뀐 쌍만
    hunk마다 묶어 돌려준다. 읽은 곳이 바뀐 쌍이 같은 PR의 답이면 그 답을 지난 판단으로 붙인다."""
    to_ask = []
    for hunk in hunks:
        missing = []
        priors: dict[str, Prior] = {}
        for rule in rules:
            entry = store.cached_result(judge_key(rule, hunk, model))
            if entry is None:
                missing.append(rule)
                continue
            reads = reads_from_json(entry.result_json)
            if not code.unchanged(reads):
                missing.append(rule)
                prior = prior_from_json(rule.id, hunk, entry.result_json, origin.pr)
                if prior is not None:
                    priors[rule.id] = replace(
                        prior, changed=code.changed_since(prior.sha, origin.sha),
                        moved=code.moved(reads),
                    )  # fmt: skip
                continue
            run.cached += 1
            why = undecided_from_json(entry.result_json)
            if why is not None:
                run.undecide(rule.id, why)
                continue
            flags = flags_from_json(rule.id, hunk, entry.result_json)
            run.flags.setdefault((hunk.file, rule.id), []).extend(flags)
        if missing:
            to_ask.append(Asked(hunk, tuple(missing), priors))
    return to_ask


def _ask(
    chunk: Sequence[Asked], *, store: Store, budget: CallBudget, model: str, code: CodeReader,
    run: _Run, origin: Origin,
) -> None:  # fmt: skip
    """한 파일의 hunk 묶음 하나를 묻는다. 저장하는 쌍의 읽은 곳은 이번에 읽은 경로이고, 지난
    판단을 준 쌍은 지난번에 읽은 경로도 더한다(지난 판단을 바탕으로 낸 답이라 그곳이 바뀌면
    다시 묻는다)."""
    call = budget.begin(PrScope(), stage="judge", tier="light", model=model)
    if isinstance(call, Denied):
        log.info("모델 판단을 하지 않음: %s", call.reason)
        run.stop = ("cap", call.reason)
        for asked in chunk:
            run.leave(asked, *run.stop)
        return
    answer = code.ask(
        build_prompt(chunk),
        system_prompt=SYSTEM_PROMPT,
        schema=ANSWER_SCHEMA,
        model=model,
        max_budget_usd=CALL_BUDGET_USD,
        max_tool_uses=MAX_TOOL_USES,
    )
    reasked = sum(len(a.priors) for a in chunk)
    run.reasked += reasked
    log.info(
        "모델 판단 호출: hunk %d개, 지난 판단을 준 쌍 %d개, 도구 %d회, 읽은 경로 %s, 입력 %s, "
        "출력 %s토큰, 거부 %d건",
        len(chunk), reasked, answer.tool_uses, list(answer.reads), answer.input_tokens,
        answer.output_tokens, answer.permission_denials,
    )  # fmt: skip
    failure = None if answer.ok else f"{answer.error}: {answer.detail}"
    parsed, reads = None, {}
    if failure is None:
        try:
            parsed = parse_answer(answer.data or {})
            prior_paths = {p for a in chunk for prior in a.priors.values() for p in prior.reads}
            reads = code.digests(sorted(set(answer.reads) | prior_paths))
        except ValueError as e:
            failure = f"답의 형식이 틀림: {str(e).splitlines()[0]}"
        except repo.GitError as e:
            failure = f"모델이 읽은 파일을 확인하지 못함: {e}"
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
    run.kept_prior += len(verified.kept_prior)
    called = {p: reads[p] for p in answer.reads if p in reads}
    for (at, rule_id), flags in verified.kept.items():
        hunk = chunk[at].hunk
        rule = next(r for r in chunk[at].rules if r.id == rule_id)
        why = verified.undecided.get((at, rule_id))
        prior = chunk[at].priors.get(rule_id)
        pair_reads = called if prior is None else {
            **called, **{p: reads[p] for p in prior.reads if p in reads}
        }  # fmt: skip
        basis = verified.basis.get((at, rule_id), "")
        if prior is not None:
            log.info("모델 판단의 지난 판단 %s: %s %s — %s",
                     "유지" if (at, rule_id) in verified.kept_prior else "바뀜", rule_id,
                     hunk.file, basis)  # fmt: skip
        saved = (
            flags_to_json(hunk, flags, pair_reads, basis=basis, origin=origin) if why is None
            else undecided_to_json(why, pair_reads, basis=basis, origin=origin)
        )  # fmt: skip
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
