"""과거 기록 저장 (docs/DESIGN.md 4.10, 6.5). 무엇을 기록할지는 app/worker/store/records_pure.py가
정한다.

- 수정안 PR이 머지·닫힘: 수정안 PR 상태를 확인하는 곳(`proposal.refresh`)에서 남긴다.
- 작성자가 자기 커밋으로 고침, 이번 검사의 위반: 새 커밋의 위반 찾기 뒤에 남긴다.
- 수정이 검증에 실패함: 수정 단계(`fix_model`)가 끝난 뒤에 남긴다.

저장 전에 비밀값 검사를 하고 걸리면 그 기록은 저장하지 않는다. 기록 실패는 PR 결과에 영향을 주지
않는다. 로그만 남기고, 같은 기록은 고유 키(repo, kind, source_id)로 거르므로 다음 기회에 다시 한다.
저장(`save`)과 수정안의 기록(`after_proposals`)은 app/worker/store/records.py에 있다.
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Final

from app.shared.db_pure import SeenViolation
from app.shared.job_pure import PRJob
from app.worker.core.tools_pure import CONFIG_RULE_ID, RUFF_CODE
from app.worker.core.violation_pure import Violation
from app.worker.fix.fix_pure import MechanicalFix
from app.worker.fix.model_fix_pure import ModelFix
from app.worker.git import repo
from app.worker.git.repo import GitError
from app.worker.rules.doc_rules_pure import Found, RuleChecked
from app.worker.sandbox.safe_read import read_regular_file
from app.worker.store import records_pure
from app.worker.store.records import _RECORD_ERRORS, save
from app.worker.store.state import Store

log = logging.getLogger(__name__)

MAX_FILE_BYTES: Final = 5 * 1024 * 1024

# 실패로 기록하는 수정 결과. 호출 상한, 실행하지 않음, 고칠 것이 없음은 실패가 아니다
_FAILED_STATUSES: Final = frozenset({
    "failed", "dropped", "new_violations", "out_of_scope", "secret", "tests_changed",
    "unchecked", "too_big",
})  # fmt: skip


def after_failed_fix(
    store: Store,
    job: PRJob,
    fix: ModelFix | MechanicalFix,
    notes: Sequence[records_pure.Target],
    worktree: Path,
    secrets: Sequence[str],
) -> None:
    """검증에 실패한 수정(모델: 회차 소진·재시도 없이 버림, 기계적 수정: 검증의 어느 단계에서
    떨어짐)의 실패 기록 (4.10). diff는 마지막 회차의 것이다."""
    if fix.status not in _FAILED_STATUSES or fix.attempt_id is None:
        return
    try:
        new = records_pure.failure_records(
            repo=job.repo, pr_number=job.pr_number, head_sha=job.head_sha,
            attempt_id=fix.attempt_id, targets=notes, patch=repo.head_diff(worktree),
            detail=fix.detail,
        )  # fmt: skip
        save(store, new, secrets)
    except _RECORD_ERRORS:
        log.exception("수정 실패 기록을 남기지 못함 %s#%s", job.repo, job.pr_number)


def mark_rejected(store: Store, job: PRJob, found: Found) -> Found:
    """이 PR에서 사람이 닫은 수정안이 고친 것과 같은 줄의 위반에 그 PR 번호를 적는다. 수정 판단의
    표 첫 줄이 이것으로 `skip`(이전에 거절됨)을 정한다 (4.6)."""
    if not found.violations:
        return found
    rejected = records_pure.rejected_lines(store.records(job.repo, job.pr_number))
    if not rejected:
        return found
    return replace(found, violations=records_pure.mark_rejected(found.violations, rejected))


def after_detect(
    store: Store,
    job: PRJob,
    found: Found,
    worktree: Path,
    secrets: Sequence[str],
    *,
    merged_fix: bool,
) -> None:
    """이전 커밋의 위반 중 작성자가 고친 것을 채택 기록으로 남기고, 이번 커밋의 위반을 기록한다.

    merged_fix는 이 커밋이 파이프라인의 수정안을 머지한 결과인지다. 그때 사라진 위반은 작성자가
    아니라 수정안이 고친 것이다(그 채택 기록은 수정안 쪽에서 남는다).
    """
    try:
        lines_of = _reader(worktree)
        if not merged_fix:
            previous = store.previous_violations(job.repo, job.pr_number, job.head_sha)
            checked = set(o.rule.id for o in found.outcomes if isinstance(o, RuleChecked))
            if CONFIG_RULE_ID in checked:  # 도구 설정 규칙의 위반은 ruff 코드가 규칙 id다 (4.3)
                checked |= {p.rule_id for p in previous if RUFF_CODE.match(p.rule_id)}
            fixed = records_pure.author_fixed(
                previous, frozenset(v.id for v in found.violations), frozenset(checked), lines_of
            )
            if fixed:
                config = next((o.rule for o in found.outcomes if o.rule.id == CONFIG_RULE_ID), None)
                texts = {o.rule.id: o.rule.text for o in found.outcomes}
                if config is not None:
                    texts |= {v.rule_id: config.text for v in fixed if RUFF_CODE.match(v.rule_id)}
                new = records_pure.author_records(
                    fixed, rule_texts=texts, head_sha=job.head_sha,
                    diff_of=_commit_diff(worktree, fixed[0].head_sha, job.head_sha),
                )  # fmt: skip
                save(store, new, secrets)
        rows = [
            SeenViolation(
                repo=job.repo,
                pr_number=job.pr_number,
                head_sha=job.head_sha,
                violation_id=v.id,
                rule_id=v.rule_id,
                file=v.file,
                line=v.line,
                line_text=v.line_text,
                message=v.message,
                unit=v.unit,
                context=records_pure.context_around(lines_of(v.file) or [], v.line),
            )  # fmt: skip
            for v in found.violations
        ]
        store.replace_violations(job.repo, job.pr_number, job.head_sha, rows)
    except _RECORD_ERRORS:
        log.exception("위반 기록을 남기지 못함 %s#%s", job.repo, job.pr_number)


def context_of(worktree: Path) -> Callable[[Violation], str]:
    """위반 줄과 앞뒤 5줄 (6.1). 검색의 질의 글(6.6의 4번)이 저장한 기록과 같은 틀이 되게 기록을
    만들 때와 같은 방법으로 읽는다. 파일을 읽지 못하면 ""."""
    lines_of = _reader(worktree)

    def context(v: Violation) -> str:
        return records_pure.context_around(lines_of(v.file) or [], v.line)

    return context


def _reader(worktree: Path) -> Callable[[str], list[str] | None]:
    cache: dict[str, list[str] | None] = {}

    def lines_of(rel: str) -> list[str] | None:
        if rel not in cache:
            path = repo.worktree_file(worktree, rel)
            data = read_regular_file(path, max_bytes=MAX_FILE_BYTES) if path is not None else None
            cache[rel] = data.decode("utf-8", errors="replace").split("\n") if data else None
        return cache[rel]

    return lines_of


def _commit_diff(worktree: Path, old: str, new: str) -> Callable[[str], str]:
    """old와 new 커밋 사이의 파일 하나의 diff. old가 bare 저장소에 없으면 ""."""

    def diff_of(rel: str) -> str:
        try:
            return repo.commit_diff(worktree, old, new, rel)
        except GitError as e:
            log.info("이전 커밋과의 diff를 읽지 못함 (%s): %s", rel, e)
            return ""

    return diff_of
