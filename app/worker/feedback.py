"""`feedback`·`resolve` 작업: PR에 달린 코멘트(Copilot 리뷰, 사람의 리뷰)를 다룬다.

- `feedback`: 이 PR의 처리하지 않은 코멘트를 지금 상태에 맞춘다. 어떤 코멘트인지는 작업에 없고
  워커가 GitHub에서 읽는다(`collect`). 웹훅으로 왔든 동기화로 왔든 같다.
- `resolve`: 수정안 PR이 머지된 뒤에 그 수정안으로 받아들인 코멘트의 스레드를 해결됨으로 바꾼다.
  웹은 머지 이벤트를 받아 큐에 넣기만 하고 바꾸는 것은 워커다. 머지되지 않고 닫힌 수정안의
  스레드는 바꾸지 않는다.
"""

import logging
from datetime import UTC, datetime

from app.shared.job_pure import PRJob
from app.worker import retry, update
from app.worker.deps import Deps
from app.worker.feedback_pure import Item, flat_item, thread_item, unhandled
from app.worker.fix import proposal
from app.worker.github import GitHub, PullRefs
from app.worker.state import Store

log = logging.getLogger(__name__)


def collect(github: GitHub, store: Store, repo: str, pr_number: int) -> list[Item]:
    """처리할 코멘트. 세 곳(줄 리뷰 스레드, 리뷰 본문, 일반 코멘트)을 읽어 다룰 것만 남긴다.

    해결된 스레드, 파이프라인 자신의 글, 명령, 권한 없는 글쓴이의 글, 이미 처리한 것은 뺀다.
    """
    items: list[Item | None] = [thread_item(t) for t in github.review_threads(repo, pr_number)]
    items += [flat_item("review", r) for r in github.reviews(repo, pr_number)]
    items += [flat_item("issue_comment", c) for c in github.issue_comments(repo, pr_number)]
    handled = {(f.kind, f.item_id) for f in store.handled_feedback(repo, pr_number)}
    return unhandled(items, handled)


def process_feedback(job: PRJob, deps: Deps) -> str:
    _, final = retry.attempt()
    admitted = update.admit(job, deps, final)
    if isinstance(admitted, str):
        return admitted  # closed, needs_approval: 할 일이 없다
    try:
        items = collect(deps.github, deps.store, job.repo, job.pr_number)
        if not items:
            return "no_feedback"
        return handle_items(job, deps, admitted, items)
    except Exception:  # 환경 오류든 버그든 재시도는 RQ가 한다
        if final:
            deps.store.release(job)
        raise


def handle_items(job: PRJob, deps: Deps, refs: PullRefs, items: list[Item]) -> str:
    """코멘트를 판단하고 받아들인 것은 고친다. 모델 판단과 수정은 다음 작업에서 넣는다.

    지금은 아무 기록도 남기지 않고 답글도 달지 않는다. 기록을 남기면 그 코멘트는 처리한 것이 되어
    `collect`가 다시 주지 않고, 동기화도 다시 넣지 않는다.
    """
    return f"feedback:pending:{len(items)}"


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
            github.resolve_thread(job.repo, row.thread_id)
            store.mark_feedback_resolved([row.id], datetime.now(UTC))
            resolved += 1
    except Exception:
        if final:
            store.release(job)
        raise
    return f"resolved:{resolved}"
