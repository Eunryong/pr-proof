"""외부 작성자 PR 가드레일의 워커 쪽 (docs/DESIGN.md 8.6, 위협 표 T2·T18).

`update`와 `command` 작업은 모두 시작에서, PR의 코드를 체크아웃하기 전에 `check`를 지난다. 작업이
어느 경로로 들어왔든(웹훅, 동기화 루프, CLI 동기화, 재등록, 재개, `/fix`) 같은 지점이다. 판단은
워커가 GitHub의 PR 조회에서 방금 읽은 값과 base의 `access`, `approvals`의 승인 기록으로만 한다.
`PRJob`에 실린 작성자 값은 기록용이다. 통과하지 못하면 `block`이 승인 안내 한 줄만 단다.
"""

import logging
from dataclasses import dataclass
from typing import Final

from app.shared.job_pure import PRJob
from app.worker.config import base_config
from app.worker.config.access_pure import AccessSettings, Author, Decision, may_run
from app.worker.deps import Deps
from app.worker.git.github import PullRefs

NEEDS_APPROVAL: Final = "needs_approval"

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Gate:
    refs: PullRefs  # 이 작업에서 GitHub에 한 번 조회한 PR
    access: AccessSettings
    decision: Decision


def author(refs: PullRefs) -> Author:
    return Author(refs.author_association, refs.author_login)


def check(job: PRJob, deps: Deps, refs: PullRefs) -> Gate:
    """체크아웃 전에 부른다. base의 설정을 읽지 못하면 GitError가 올라가 실행하지 않는다."""
    access = base_config.load_access(deps.settings.data_dir, job.repo, refs.base_sha)
    approved = deps.store.approved_shas(job.repo, job.pr_number)
    decision = may_run(refs.author_association, refs.author_login, refs.head_sha, access, approved)
    if job.author_association is not None and job.author_association != refs.author_association:
        log.info(
            "작업에 실린 작성자 값과 GitHub의 값이 다름 (판단은 GitHub의 값): %s#%d %r → %r",
            job.repo, job.pr_number, job.author_association, refs.author_association,
        )  # fmt: skip
    return Gate(refs, access, decision)


def block(job: PRJob, deps: Deps, refs: PullRefs) -> str:
    """승인 안내 한 줄만 달고 끝낸다. 체크아웃, 컨테이너, 모델 호출이 없다. 막은 사실은 runs에
    남긴다.

    결과 코멘트와 같은 표식의 코멘트 하나를 고쳐 쓰므로 새 커밋이 와도 한 줄이고 해시만 바뀐다.
    PR의 내용(제목, 브랜치 이름)은 읽지도 넣지도 않는다.
    """
    log.warning(
        "신뢰되지 않은 작성자의 PR이라 승인을 기다림: %s#%d 작성자 %r (%r), head %s",
        job.repo, job.pr_number, refs.author_login, refs.author_association, refs.head_sha[:7],
    )  # fmt: skip
    store = deps.store
    comment_id = deps.github.upsert_comment(
        job.repo, job.pr_number, approval_guide(refs.head_sha),
        store.last_comment_id(job.repo, job.pr_number),
    )  # fmt: skip
    store.record_blocked(job, refs.head_sha, author(refs), comment_id)
    return NEEDS_APPROVAL


def approval_guide(head_sha: str) -> str:
    """승인 명령은 전체 해시만 받으므로(T31) 안내에도 그대로 복사할 수 있게 전체 해시를 적는다."""
    return (
        "외부 작성자의 PR이라 검사하지 않았습니다. 저장소 멤버가 코드를 확인한 뒤 "
        f"`/recheck {head_sha}`를 달면 이 커밋을 검사합니다."
    )


def missing_commit_reply(head_sha: str) -> str:
    return f"> /recheck\n\n{approval_guide(head_sha)}"


def bad_commit_reply(head_sha: str) -> str:
    return (
        "> /recheck\n\n커밋은 전체 해시 40자로 적어 주세요. 짧은 해시로는 승인하지 않습니다. "
        f"지금 head는 `{head_sha}`입니다. 확인한 뒤 `/recheck {head_sha}`을 달아 주세요."
    )


def head_moved_reply(head_sha: str) -> str:
    return (
        f"> /recheck\n\n새 커밋이 왔습니다. 지금 head는 `{head_sha}`입니다. "
        f"확인한 뒤 `/recheck {head_sha}`을 달아 주세요."
    )
