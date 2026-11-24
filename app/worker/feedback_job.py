"""`feedback`·`resolve` 작업: PR에 달린 코멘트(Copilot 리뷰, 사람의 리뷰)를 다룬다 (DESIGN.md 4.11).

- `feedback`: 이 PR의 처리하지 않은 코멘트를 지금 상태에 맞춘다. 어떤 코멘트인지는 작업에 없고
  워커가 GitHub에서 읽는다(`collect`). 웹훅으로 왔든 동기화로 왔든 같다. 흐름은 규칙 검사의
  그래프와 따로 둔 평범한 함수들이다: 겹침 가리기 → (남은 것이 있으면) 판단 자료 읽기 → 판단 →
  (받아들인 것이 있으면) 체크아웃 → 테스트 기준선 → 수정 → 전달 → 답글과 기록.
- `resolve`: 수정안 PR이 머지된 뒤에 그 수정안으로 받아들인 코멘트의 스레드를 해결됨으로 바꾼다.
  웹은 머지 이벤트를 받아 큐에 넣기만 하고 바꾸는 것은 워커다. 머지되지 않고 닫힌 수정안의
  스레드는 바꾸지 않는다.

받은 코멘트는 모두 기록을 남기고 끝난다(받아들임, 거절, 할 일 없음, 미룸, 오류). 남기지 않으면
동기화가 5분마다 다시 넣는다. 답글을 단 뒤 바로 기록하므로, 다시 시도해도 기록한 코멘트는
`collect`가 주지 않아 같은 답글이 두 번 달리지 않는다. 새 커밋이 와서 멈추면 그 코멘트는 기록하지
않고 새 커밋의 작업에 맡긴다.
"""

import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Literal

from app.shared.feedback_pure import handled, resolution_reason
from app.shared.job_pure import PRJob
from app.shared.other_server_pure import skips
from app.worker import graph, retry, update
from app.worker.config import base_config
from app.worker.config.access_pure import Author, is_trusted
from app.worker.config.repo_config_pure import RepoConfig, call_limits
from app.worker.core.bundle_pure import FEEDBACK_BUNDLE
from app.worker.core.rule_pure import Rule
from app.worker.deps import Deps
from app.worker.feedback import feedback_triage
from app.worker.feedback.feedback_pure import Item, flat_item, thread_item, unhandled
from app.worker.feedback.feedback_report_pure import (
    RESOLVES,
    RESOLVES_FOR_COPILOT,
    VERDICTS,
    Handled,
    record_why,
    reply_body,
)
from app.worker.feedback.feedback_triage_pure import Facts, KnownFix, Triage, excerpt, overlaps
from app.worker.fix import model_fix, proposal
from app.worker.fix.feedback_fix_pure import FIX_NOTE, Accepted, feedback_found, fix_targets
from app.worker.fix.fix_decision_pure import SafetyNet, safety_gap
from app.worker.fix.model_fix_pure import Picked
from app.worker.git import repo
from app.worker.git.git_pure import REGULAR_FILE_MODES
from app.worker.git.github import GitHub, PullRefs
from app.worker.lang import python
from app.worker.model.model_backend import AskModel, fix_unavailable
from app.worker.rules import detect, doc_rules, rules
from app.worker.rules.checker_authoring_pure import Checked
from app.worker.rules.detect import Detection, source_lines
from app.worker.rules.doc_rules_pure import Found
from app.worker.rules.rule_docs_source_pure import read_commit, source_for
from app.worker.store.budget import CallBudget
from app.worker.store.records_pure import targets_from_json
from app.worker.store.state import Store
from app.worker.testing.test_run_pure import PytestRun

log = logging.getLogger(__name__)

# 한 번에 다루는 코멘트 수. 나머지는 기록하지 않고 남겨 다음 작업(동기화)이 다룬다
MAX_ITEMS_PER_RUN: Final = 30
MAX_COMMITS: Final = 30
MAX_SOURCE_BYTES: Final = 1024 * 1024
NEW_COMMIT: Final = "그 사이 새 커밋이 올라옴"
FORK_NO_PR: Final = "포크에서 온 PR이라 수정안 PR을 열 수 없음"


def collect(github: GitHub, store: Store, repo: str, pr_number: int, head_sha: str) -> list[Item]:
    """처리할 코멘트. 세 곳(줄 리뷰 스레드, 리뷰 본문, 일반 코멘트)을 읽어 다룰 것만 남긴다.

    해결된 스레드, 파이프라인 자신의 글, 명령, 권한 없는 글쓴이의 글, 이미 처리한 것은 뺀다. 처리한
    것인지는 동기화와 같은 `feedback_pure.handled`로 정한다(head_sha는 지금 PR head).
    """
    items: list[Item | None] = [thread_item(t) for t in github.review_threads(repo, pr_number)]
    items += [flat_item("review", r) for r in github.reviews(repo, pr_number)]
    items += [flat_item("issue_comment", c) for c in github.issue_comments(repo, pr_number)]
    superseded = {p.number for p in store.proposals(repo, pr_number, state="superseded")}
    done = {
        (f.kind, f.item_id) for f in store.handled_feedback(repo, pr_number)
        if handled(f.verdict, f.head_sha, head_sha, f.proposal_number in superseded)
    }  # fmt: skip
    return unhandled(items, done)


def process_feedback(job: PRJob, deps: Deps) -> str:
    _, final = retry.attempt()
    admitted = update.admit(job, deps, final)
    if isinstance(admitted, str):
        return admitted  # closed, needs_approval: 할 일이 없다
    try:
        items = collect(deps.github, deps.store, job.repo, job.pr_number, admitted.head_sha)
        if not items:
            return "no_feedback"
        # 판이 같거나 높은 다른 서버가 결과 코멘트의 주인이면 그 서버가 이 PR의 코멘트도 다룬다.
        # 기록을 남기지 않으므로 이 서버가 넘겨받으면 그때 다룬다
        current = job.model_copy(update={"head_sha": admitted.head_sha})
        found = deps.github.result_comment(job.repo, job.pr_number)
        if skips(update.other_server(current, deps, found)):
            return "other_server"
        return handle_items(job, deps, admitted, items)
    except Exception:  # 환경 오류든 버그든 재시도는 RQ가 한다
        if final:
            deps.store.release(job)
        raise


# --- 판단 자료 -------------------------------------------------------------------------


@dataclass(frozen=True)
class Material:
    """판단 모델에 주는 자료. 체크아웃 없이 bare 캐시와 저장된 규칙 묶음에서 읽는다."""

    config: RepoConfig
    rules: tuple[Rule, ...]
    rules_note: str  # 규칙이 없거나 읽지 못한 까닭. 규칙이 있으면 ""
    facts: Facts
    code: dict[int, str] = field(default_factory=dict)  # item_id → 줄 둘레의 지금 코드


def _budget(deps: Deps, work: PRJob, config: RepoConfig) -> CallBudget:
    limits = call_limits(config.model.calls, **deps.settings.model.calls.model_dump())
    return CallBudget(deps.store, work.repo, work.pr_number, work.head_sha, limits)


def _rules_of(result: rules.RulesResult) -> tuple[tuple[Rule, ...], str]:
    match result:
        case rules.RulesFound(rules=found):
            return found, ""
        case rules.NoRuleDocuments():
            return (), "규칙 문서가 없음"
        case rules.NoRules():
            return (), "규칙 문서에서 규칙을 찾지 못함"
        case rules.ModelFailed(reason=reason):
            return (), f"규칙을 읽지 못함 ({reason[:200]})"
        case rules.CapReached(denied=denied):
            return (), f"규칙을 읽지 않음 ({denied.reason})"
    return (), ""


def _head_text(bare, head_sha: str, path: str) -> str:
    entry = repo.bare_entries(bare, head_sha, [path]).get(path)
    if (entry is None or entry.mode not in REGULAR_FILE_MODES or entry.size is None
            or entry.size > MAX_SOURCE_BYTES):  # fmt: skip
        return ""
    try:
        return repo.bare_blob(bare, entry.sha)
    except UnicodeDecodeError:
        return ""


def read_material(
    deps: Deps,
    work: PRJob,
    items: Sequence[Item],
    ask: AskModel,
    *,
    author: Author | None = None,
) -> Material:
    """판단 자료를 읽는다. 규칙 묶음은 규칙 검사가 저장한 것을 쓰고(키는 규칙 검사가 읽은 커밋의
    규칙 문서), 없으면 그때만 체크아웃해 규칙 읽기를 한 번 한다(테스트는 돌리지 않는다). 그 결과도
    같은 키로 저장돼 규칙 검사가 다시 쓴다. author는 GitHub에서 읽은 PR 작성자다. 규칙 문서를 읽는
    커밋을 규칙 검사와 같은 기준으로 정하는 데 쓴다."""
    data_dir, store = deps.settings.data_dir, deps.store
    bare = repo.ensure_bare(data_dir, work.repo)
    repo.fetch_pr(bare, work.pr_number, work.base_sha)
    config = base_config.load_repo(
        data_dir, work.repo, work.base_sha, default_model=deps.settings.model.default
    )
    model = config.model.rules
    access = base_config.load_access(data_dir, work.repo, work.base_sha)
    fork = repo.bare_merge_base(bare, work.base_sha, work.head_sha)
    source = source_for(
        config.rules.docs,
        repo.bare_entries(bare, fork, config.rules.docs),
        repo.bare_entries(bare, work.head_sha, config.rules.docs),
        trusted=is_trusted(author, access), rules_from=config.rules.docs_from,
    )  # fmt: skip
    docs_sha = read_commit(source, work.base_sha, work.head_sha)
    saved = rules.saved_rules(bare, docs_sha, config.rules, model=model, store=store)
    if saved is None:
        with repo.checkout(data_dir, work.repo, work.pr_number, work.head_sha,
                           work.base_sha) as co:  # fmt: skip
            saved = rules.load_rules(
                co.path, docs_sha, config.rules, model=model, store=store,
                budget=_budget(deps, work, config), ask=ask,
            )  # fmt: skip
    found, note = _rules_of(saved)
    text = deps.github.pr_text(work.repo, work.pr_number)
    commits, files = repo.bare_pr_facts(bare, work.base_sha, work.head_sha, limit=MAX_COMMITS)
    texts = {
        path: _head_text(bare, work.head_sha, path) for path in {i.path for i in items if i.path}
    }
    code = {
        i.item_id: excerpt(texts[i.path].split("\n"), i.line)
        for i in items if i.path and texts[i.path]
    }  # fmt: skip
    return Material(
        config, found, note, Facts(text.title, text.body, tuple(commits), tuple(files)), code
    )


def known_fixes(store: Store, repo_name: str, pr_number: int) -> list[KnownFix]:
    """이 PR의 열린·머지된 수정안(묶음 무관)이 고친 대상 위반. 수정 시도의 기록에서 읽는다."""
    out = []
    for p in store.proposals(repo_name, pr_number):
        if p.state not in ("open", "merged") or p.attempt_id is None:
            continue
        attempt = store.fix_attempt(p.attempt_id)
        if attempt is None:
            continue
        out += [
            KnownFix(p.number, p.state, p.head_sha, t.file, t.line, t.rule_id)
            for t in targets_from_json(attempt.targets_json)
        ]
    return out


# --- 수정 -----------------------------------------------------------------------------


@dataclass(frozen=True)
class FixResult:
    outcome: Literal["fixed", "deferred", "failed"]
    number: int | None = None
    reason: str = ""


def _open_feedback_proposal(store: Store, work: PRJob) -> int | None:
    for p in store.proposals(work.repo, work.pr_number, state="open"):
        if p.skill == FEEDBACK_BUNDLE and p.head_sha == work.head_sha:
            return p.number
    return None


def fix_accepted(
    work: PRJob, deps: Deps, refs: PullRefs, accepted: Sequence[Accepted]
) -> FixResult | None:
    """받아들인 코멘트를 수정 작성의 틀(`model_fix.fix_violations`)로 한 번에 고치고, 출구 검사를
    통과하면 수정안 PR 하나로 올린다. 새 커밋이 와서 멈추면 None.

    포크 PR, 이 커밋에 이미 열린 코멘트 수정안, 테스트가 실패했거나 없음은 고치지 않고 미룬다.
    체크아웃과 테스트 기준선은 여기서만 한다(고칠 코멘트가 있을 때만). 테스트 기준선은 줄마다 있던
    테스트가 실행하는지(만든 테스트가 필요한지)를 알려고 다시 돌린다.
    """
    store = deps.store
    if refs.head_repo != work.repo:
        return FixResult("deferred", reason=FORK_NO_PR)
    number = _open_feedback_proposal(store, work)
    if number is not None:
        return FixResult("deferred", reason=f"이 커밋에 열린 코멘트 수정안 #{number}이 있음")
    ctx = update.flow_context(deps, run_id=0, comment_id=None)
    checked = graph.checked_values(work, ctx)
    before = checked.get("test_run")
    if isinstance(before, PytestRun):  # 같은 커밋의 규칙 검사가 이미 본 테스트 결과
        gap = safety_gap(SafetyNet(before.status, flaky=bool(before.flaky)))
        if gap is not None:
            return FixResult("deferred", reason=gap)
    base = checked.get("doc_rules")
    base = base if isinstance(base, Found) else None
    with ctx.stack:
        co = ctx.stack.enter_context(repo.checkout(
            ctx.data_dir, work.repo, work.pr_number, work.head_sha, work.base_sha
        ))  # fmt: skip
        ctx.worktree, ctx.merge_base = co.path, co.merge_base
        ctx.base = base_config.load(
            co.path, work.base_sha, ctx.max_memory_gb, default_model=ctx.default_model
        )
        run = graph.pr_tests(ctx, work)
        if run.cancelled:
            return None
        gap = safety_gap(SafetyNet(run.status, flaky=bool(run.flaky)))
        if gap is not None:
            return FixResult("deferred", reason=gap)
        files = sorted({f for a in accepted for f in a.triage.files})
        violations, new_rules = fix_targets(accepted, {f: source_lines(co.path, f) for f in files})
        violations = list(python.annotate_coverage(violations, run.coverage_xml, co.path))
        found = feedback_found(base, violations, new_rules)
        picked = Picked(tuple(violations), ())
        outcome = _write_fix(ctx, work, deps, found, picked, run, base, len(accepted))
        if outcome is None:
            return None
        if not outcome.ready:
            return FixResult("failed", reason=outcome.detail or outcome.status)
        # 전달은 새 worktree에 저장한 패치를 적용하고 해시를 맞춰 본 뒤 올린다 (4.8 끝)
        fresh = ctx.stack.enter_context(repo.checkout(
            ctx.data_dir, work.repo, work.pr_number, work.head_sha, work.base_sha
        ))  # fmt: skip
        stored = model_fix.reapply(store, outcome.attempt_id, fresh.path)
        if isinstance(stored, str):
            return FixResult("failed", reason=stored)
        result = proposal.open_feedback_proposal(
            work, fresh.path, store, deps.github, outcome, found, accepted,
            patch=stored.patch or "",
        )  # fmt: skip
    if result.reason == NEW_COMMIT:
        return None
    if result.number is None:
        return FixResult("failed", reason=result.reason)
    return FixResult("fixed", number=result.number)


def _write_fix(ctx: graph.FlowContext, work: PRJob, deps: Deps, found: Found, picked: Picked,
               run: PytestRun, base: Found | None, count: int):  # fmt: skip
    """`model_fix.fix_violations`를 부르고 시도를 저장한다. 규칙 재검사는 같은 커밋의 규칙 검사가
    채택한 검사기로 한다(그 결과가 없으면 돌릴 검사기가 없다)."""
    store, settings = deps.store, ctx.base.repo
    checks = doc_rules.saved_checks(base, store) if base is not None else []
    sandbox = doc_rules.sandbox_for(
        ctx.data_dir / graph.TOOL_DIR, ctx.base.ruff, ctx.sandbox_names.image
    )
    disabled = frozenset(settings.rules.disabled)

    def recheck(changed, new) -> Detection:
        if not checks:
            return Detection(())
        return detect.detect(checks, ctx.worktree, changed, new_files=new, sandbox=sandbox,
                             disabled=disabled)  # fmt: skip

    tools = model_fix.FixTools(
        run_model=deps.backend.fix, unavailable=fix_unavailable(deps.backend), recheck=recheck,
        rules_rechecked=sum(1 for c in checks if isinstance(c, Checked)),
        run_suite=lambda generated, files: graph.pr_suite(ctx, work, generated, files),
        is_stale=lambda: store.is_stale(work), secrets=proposal.pipeline_secrets(),
    )  # fmt: skip
    notes = model_fix.notes_for(found, picked, ctx.worktree)
    outcome = model_fix.fix_violations(
        found, picked, worktree=ctx.worktree, merge_base=ctx.merge_base, test_before=run,
        store=store, budget=_budget(deps, work, settings).with_targets(count),
        models=settings.model, max_attempts=settings.fix.max_retries, tools=tools,
        generation=store.call_grants(work.repo, work.pr_number), note=FIX_NOTE,
    )  # fmt: skip
    if outcome is None:
        return None
    return model_fix.save_attempt(
        store, work, ctx.merge_base, outcome, notes, ctx.worktree, bundle=FEEDBACK_BUNDLE
    )


# --- 답글과 기록 -----------------------------------------------------------------------


def _finish(deps: Deps, work: PRJob, item: Item, h: Handled) -> None:
    """답글을 달고(달 것이 있으면), 이미 고쳐진 것이면 스레드를 해결하고, 바로 기록한다."""
    github, store = deps.github, deps.store
    body = reply_body(item, h)
    reply_id = None
    if body is not None:
        if item.kind == "review_comment":
            reply_id = github.reply_to_review_comment(work.repo, work.pr_number, item.item_id, body)
        else:
            reply_id = github.feedback_reply(work.repo, work.pr_number, body)
    resolved_at = None
    reason = resolution_reason(item.login, rejected=h.outcome == "rejected", incorrect=h.incorrect)
    resolves = RESOLVES_FOR_COPILOT if reason is not None else RESOLVES
    if h.outcome in resolves and item.thread_id is not None:
        github.resolve_thread(work.repo, item.thread_id, reason)
        resolved_at = datetime.now(UTC)
    verdict = VERDICTS[h.outcome]
    store.record_feedback(
        repo=work.repo, pr_number=work.pr_number, kind=item.kind, item_id=item.item_id,
        thread_id=item.thread_id, login=item.login, head_sha=work.head_sha, verdict=verdict,
        why=record_why(h), reply_id=reply_id,
        proposal_number=h.number if verdict == "accepted" else None, resolved_at=resolved_at,
    )  # fmt: skip


def _from_triage(t: Triage) -> Handled | None:
    """판단 하나의 결과. accept는 고친 뒤에 정하므로 None."""
    match t.verdict:
        case "accept":
            return None
        case "already_proposed":
            return Handled("proposed", why=t.why, number=t.number)
        case "already_fixed":
            return Handled("already_fixed", why=t.why, number=t.number)
        case "reject":
            return Handled("rejected", why=t.why, incorrect=t.incorrect)
        case "no_action":
            return Handled("no_action", why=t.why)
    return Handled("error", reason=t.why)


def handle_items(
    job: PRJob, deps: Deps, refs: PullRefs, items: list[Item], *, ask: AskModel | None = None
) -> str:
    """코멘트를 판단하고 받아들인 것은 고친다. 결과는 처리 결과별 수다(예 `feedback:rejected=1`).

    1. 겹침: 수정안이 고친 줄이나 지금 커밋의 규칙 검사가 찾은 줄과 같은 줄 코멘트는 모델을 부르지
       않고 끝낸다(`overlaps`).
    2. 판단: 남은 코멘트를 한 번에 묻는다. 자료는 체크아웃 없이 읽는다.
    3. 수정: 받아들인 코멘트가 있을 때만 체크아웃하고 한 수정안으로 고친다.
    ask는 판단 모델 호출이다(기본은 모델 구현의 `ask`).
    """
    ask = ask or deps.backend.ask
    store = deps.store
    store.set_latest_head(job.repo, job.pr_number, refs.head_sha)
    work = job.model_copy(update={"head_sha": refs.head_sha, "base_sha": refs.base_sha})
    items = items[:MAX_ITEMS_PER_RUN]
    counts: Counter[str] = Counter()

    def finish(item: Item, h: Handled) -> None:
        _finish(deps, work, item, h)
        counts[h.outcome] += 1

    # 머지 웹훅을 놓쳤어도 머지된 수정안이 고친 줄은 머지된 것으로 가린다
    proposal.refresh(store, deps.github, work.repo, work.pr_number)
    fixes = known_fixes(store, work.repo, work.pr_number)
    flagged = frozenset(
        (v.file, v.line) for v in store.violations_at(work.repo, work.pr_number, work.head_sha)
    )
    covered = overlaps(items, fixes, flagged, work.head_sha)
    for item in items:
        if item.item_id in covered:
            o = covered[item.item_id]
            finish(item, Handled(o.kind, number=o.number))
    rest = [i for i in items if i.item_id not in covered]
    if rest:
        author = Author(refs.author_association, refs.author_login)
        material = read_material(deps, work, rest, ask, author=author)
        decided = feedback_triage.triage(
            rest, rules=material.rules, rules_note=material.rules_note, facts=material.facts,
            code=material.code, fixes=fixes, store=store,
            budget=_budget(deps, work, material.config), model=material.config.model.decide,
            ask=ask,
        )  # fmt: skip
        accepted = []
        for item in rest:
            t = decided[item.item_id]
            h = _from_triage(t)
            if h is None:
                accepted.append(Accepted(item, t))
            else:
                finish(item, h)
        if accepted:
            result = fix_accepted(work, deps, refs, accepted)
            if result is None:
                counts["stale"] += len(accepted)  # 기록하지 않는다. 새 커밋의 작업이 다시 본다
            else:
                for a in accepted:
                    finish(a.item, Handled(result.outcome, why=a.triage.why,
                                           reason=result.reason, number=result.number))  # fmt: skip
    return "feedback:" + ",".join(f"{k}={n}" for k, n in sorted(counts.items()))


def process_resolve(job: PRJob, deps: Deps) -> str:
    """머지된 수정안으로 받아들인 코멘트의 스레드를 해결됨으로 바꾼다. job.pr_number는 원래 PR이다.

    수정안의 상태는 웹훅의 내용이 아니라 GitHub에서 다시 읽어 정한다. 닫힌 PR이어도 한다(수정안을
    머지한 뒤 원래 PR도 머지됐을 수 있다). 스레드마다 바로 적어, 중간에 실패해 다시 해도 해결한
    스레드는 건너뛴다.
    """
    _, final = retry.attempt()
    store, github = deps.store, deps.github
    try:
        proposal.refresh(store, github, job.repo, job.pr_number)
        merged = {p.number for p in store.proposals(job.repo, job.pr_number, state="merged")}
        resolved = 0
        for row in store.feedback_awaiting_merge(job.repo, job.pr_number):
            if row.proposal_number not in merged:
                continue  # 아직 열려 있거나 머지되지 않고 닫힌 수정안
            assert row.thread_id is not None and row.id is not None
            github.resolve_thread(job.repo, row.thread_id, resolution_reason(row.login))
            store.mark_feedback_resolved([row.id], datetime.now(UTC))
            resolved += 1
    except Exception:
        if final:
            store.release(job)
        raise
    return f"resolved:{resolved}"
