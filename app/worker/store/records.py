"""과거 기록 저장 (docs/DESIGN.md 4.10, 6.5): 비밀값 검사를 거친 저장과 수정안의 채택·거절 기록.
무엇을 기록할지는 app/worker/store/records_pure.py가 정하고, 단계의 결과(위반 찾기, 수정 실패)를
기록으로 바꾸는 것은 app/worker/records.py가 한다.

저장 전에 비밀값 검사를 하고 걸리면 그 기록은 저장하지 않는다. 기록 실패는 PR 결과에 영향을 주지
않는다. 로그만 남기고, 같은 기록은 고유 키(repo, kind, source_id)로 거르므로 다음 기회에 다시 한다.
"""

import logging
from collections.abc import Iterable, Mapping, Sequence
from typing import Final

from sqlalchemy.exc import SQLAlchemyError

from app.worker.core.bundle_pure import FEEDBACK_BUNDLE
from app.worker.core.secret_pure import find_secret_in_text
from app.worker.git.repo import GitError
from app.worker.store import records_pure
from app.worker.store.state import Store

log = logging.getLogger(__name__)

# 기록을 만들다 날 수 있는 오류. 그 밖의 예외는 파이프라인 버그라 그대로 올린다
_RECORD_ERRORS: Final = (SQLAlchemyError, GitError, OSError, ValueError)
# 머지·닫힘을 기록하는 수정안 상태
_FINISHED_STATES: Final[Mapping[str, str]] = {"merged": "adopted", "rejected": "rejected"}


def save(store: Store, new: Iterable[records_pure.NewRecord], secrets: Sequence[str]) -> int:
    """비밀값 검사를 거친 기록만 저장한다. 새로 저장한 수."""
    saved = 0
    for record in new:
        kind = find_secret_in_text(records_pure.text_of(record), secrets)
        if kind is not None:
            log.warning(
                "기록을 저장하지 않음 %s %s: 비밀값 (%s)", record.repo, record.source_id, kind
            )
            continue
        saved += store.add_record(record)
    return saved


def after_proposals(store: Store, repo_name: str, pr_number: int, secrets: Sequence[str]) -> None:
    """이 PR에서 머지됐거나 사람이 닫은 문서 규칙 수정안마다 채택·거절 기록을 남긴다.

    이미 남긴 기록은 고유 키로 걸러지므로, 앞에서 실패했던 것도 여기서 다시 남는다."""
    try:
        for p in store.proposals(repo_name, pr_number):
            kind = _FINISHED_STATES.get(p.state)
            if p.skill == FEEDBACK_BUNDLE:  # 코멘트 수정안은 문서 규칙의 기록을 만들지 않는다
                continue
            attempt = store.fix_attempt(p.attempt_id) if p.attempt_id is not None else None
            if kind is None or attempt is None or attempt.patch is None:
                continue
            new = records_pure.fix_pr_records(
                kind, repo=repo_name, pr_number=pr_number, fix_pr_number=p.number,
                commit_sha=p.merge_commit if kind == "adopted" else p.head_sha,
                targets=records_pure.targets_from_json(attempt.targets_json), patch=attempt.patch,
            )  # fmt: skip
            save(store, new, secrets)
    except _RECORD_ERRORS:
        log.exception("수정안의 기록을 남기지 못함 %s#%s", repo_name, pr_number)
