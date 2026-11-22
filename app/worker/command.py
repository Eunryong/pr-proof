"""`command` 작업: PR 코멘트 명령 처리 (docs/DESIGN.md 4.9 명령).

- `/recheck`: PR의 지금 head로 다시 검사한다. 신뢰되지 않은 작성자의 PR에서는 커밋을 적은
  `/recheck <커밋>`만 승인이고, 그 커밋이 지금 head일 때만 그 커밋을 검사한다 (8.6)
- `/fix <묶음>`: 승인을 기다리는 수정을 승인하고, 이 커밋에서 수정안을 내지 못하고 끝난 수정(검증
  실패, 호출 상한)을 다시 돌린다. 묶음은 `mechanical`·`model`이고, 인자가 없으면 할 일이 남은 묶음
  전부다. v1의 `/fix lint`는 3단계에 지원하지 않는 명령이 되었다 (그 경로가 `mechanical`이 되었다)
- 모르는 명령은 답하지 않는다 (`/cc` 같은 다른 관례일 수 있다)

처리한 코멘트 ID는 runs와 따로 저장해 같은 코멘트에 두 번 답하지 않는다.
"""

import logging

from app.shared.job_pure import PRJob
from app.worker import graph, guard, retry, update
from app.worker.access_pure import (
    BadCommit,
    Commit,
    NeedsApproval,
    NoCommit,
    approval_arg,
    may_approve,
)
from app.worker.command_pure import FIX_TARGETS, parse_command
from app.worker.deps import Deps
from app.worker.fix import proposal
from app.worker.github import PullRefs

log = logging.getLogger(__name__)


def fix_unsupported_message(word: str, arg: str) -> str:
    target = f"{word} {arg}".strip()
    return (
        f"> {target}\n\n`{target}`는 지원하지 않습니다. "
        "`/fix`, `/fix mechanical`(도구의 자동 수정), `/fix model`(모델 수정)을 쓸 수 있습니다."
    )


def no_pending_fix_message(
    head_sha: str, history: str = "", number: int | None = None, skill: str = "",
    typed: str = "",
) -> str:  # fmt: skip
    """재개할 수정이 없을 때의 답글. 이 커밋의 수정안 PR 기록이 있으면 그 번호를 알려준다.

    typed는 코멘트에 쓴 인자다. 비면 skill로 쓴다.
    """
    commit = f"이 커밋(`{head_sha[:7]}`)"
    if history == "rejected" and number is not None:
        reason = (
            f"{commit}의 {skill} 제안 PR #{number}이 머지되지 않고 닫혀 다시 제안하지 않습니다. "
            "새 커밋을 올리면 다시 검사합니다."
        )
    elif history == "open" and number is not None:
        reason = f"{commit}의 {skill} 제안 PR #{number}이 이미 열려 있습니다."
    elif history == "merged" and number is not None:
        reason = (
            f"{commit}은 {skill} 제안 PR #{number}을 머지한 결과라 "
            "같은 제안을 다시 만들지 않습니다."
        )
    else:
        what = f"{skill} 수정" if skill else "수정"
        reason = (
            f"{commit}에는 승인을 기다리거나 다시 돌릴 {what}이 없습니다. "
            "자동으로 고칠 수 있는 위반이 없거나, 테스트가 통과하지 않았거나, "
            "이미 수정안을 만들었을 수 있습니다."
        )
    return f"> {f'/fix {typed or skill}'.strip()}\n\n{reason}"


def process_command(job: PRJob, deps: Deps) -> str:
    assert job.command is not None and job.comment_id is not None
    if deps.store.command_handled(job.comment_id):
        return "duplicate"
    parsed = parse_command(job.command)
    if parsed.name == "unknown":
        # 답하지 않지만 처리한 것으로 적는다. 적지 않으면 동기화가 주기마다 다시 넣는다
        deps.store.record_command(job, "unknown_command")
        return "unknown_command"
    _, final = retry.attempt()
    try:
        refs = deps.github.pr_refs(job.repo, job.pr_number)
        gate = guard.check(job, deps, refs) if refs.open else None
        if gate is None:
            result = "closed"
        elif isinstance(gate.decision, NeedsApproval):
            # `/fix`는 누가 달아도 승인이 아니다. 승인은 커밋을 적은 `/recheck <커밋>`뿐이다
            if parsed.name == "recheck":
                result = _approve(job, deps, gate)
            else:
                result = guard.block(job, deps, refs)
        elif parsed.name == "recheck":
            # `/recheck`는 수정안의 머지 커밋이어도 옮겨 적지 않고 처음부터 검사한다
            result = f"recheck:{update.run_admitted(_target(job, refs), deps, refs, carry=False)}"
        else:
            result = _fix(job, deps, parsed.word, parsed.arg, refs)
    except Exception:  # 환경 오류든 버그든 재시도는 RQ가 한다
        if final:
            # 마지막 시도면 실패로 적고 키를 지운다. 적지 않으면 동기화가 주기마다 다시 넣는다.
            # 다시 하려면 명령을 새로 단다 (update의 error와 같은 규칙)
            deps.store.record_command(job, "error")
            deps.store.release(job)
        raise
    deps.store.record_command(job, result)
    return result


def _approve(job: PRJob, deps: Deps, gate: guard.Gate) -> str:
    """신뢰되지 않은 작성자의 PR에 달린 `/recheck`. 승인이면 기록하고 그 커밋 하나를 검사한다.

    승인자와 명령의 글은 작업에 실린 값이 아니라 지금 GitHub에서 읽은 코멘트로 확인한다. 승인할
    수 없는 사람이면 다른 신뢰되지 않은 명령처럼 답하지 않는다. 커밋이 없거나, 전체 해시(40자)가
    아니거나, 지금 head가 아니면 실행하지 않고 답한다. 답에는 코멘트에 쓴 글을 옮기지 않는다.
    """
    assert job.comment_id is not None
    refs = gate.refs
    comment = deps.github.issue_comment(job.repo, job.comment_id)
    if comment.pr_number != job.pr_number or not may_approve(
        comment.association, comment.login, gate.access
    ):
        log.warning(
            "승인할 수 없는 사람의 /recheck라 무시함: %s#%d 코멘트 %d %r (%r)",
            job.repo, job.pr_number, job.comment_id, comment.login, comment.association,
        )  # fmt: skip
        return "approver_not_trusted"
    lines = comment.body.strip().splitlines()
    parsed = parse_command(lines[0] if lines else "")
    if parsed.name != "recheck":
        return "approval_comment_changed"  # 큐에서 기다리는 사이에 코멘트가 고쳐졌다
    match approval_arg(parsed.arg):
        case NoCommit():
            deps.github.reply(job.repo, job.pr_number, guard.missing_commit_reply(refs.head_sha))
            return "approval_missing_commit"
        case BadCommit():
            deps.github.reply(job.repo, job.pr_number, guard.bad_commit_reply(refs.head_sha))
            return "approval_bad_commit"
        case Commit() as commit if not commit.matches(refs.head_sha):
            deps.github.reply(job.repo, job.pr_number, guard.head_moved_reply(refs.head_sha))
            return "approval_head_moved"
    deps.store.add_approval(job.repo, job.pr_number, refs.head_sha, comment.login, job.comment_id)
    return f"approved:{update.run_admitted(_target(job, refs), deps, refs, carry=False)}"


def _target(job: PRJob, refs: PullRefs) -> PRJob:
    """명령이 가리키는 PR의 지금 커밋에 대한 update 작업. 작업에 실린 작성자 값(기록용)은 옮긴다."""
    return PRJob(
        repo=job.repo,
        pr_number=job.pr_number,
        kind="update",
        head_sha=refs.head_sha,
        base_sha=refs.base_sha,
        source=job.source,
        author_association=job.author_association,
        author_login=job.author_login,
    )


def _fix(job: PRJob, deps: Deps, word: str, arg: str, refs: PullRefs) -> str:
    """arg의 묶음마다 재개할 수정이 있으면 차례로 이어 간다. 하나도 없으면 그 이유를 답한다.

    모델 묶음을 다시 돌릴 때는 PR 호출 상한을 한 번 더 준다 (9.2). 같은 코멘트를 다시 처리해도
    (재시도) 한 번이고, 이미 처리한 묶음은 재개할 것이 없어 건너뛴다.
    """
    target = _target(job, refs)
    skills = FIX_TARGETS.get(arg)
    if skills is None:
        deps.github.reply(job.repo, job.pr_number, fix_unsupported_message(word, arg))
        return "fix_unsupported"
    results = []
    for skill in skills:
        pending = update.pending_fix(target, deps, skill)
        if pending is None:
            continue
        if skill == graph.MODEL and pending == "rerun":
            deps.store.add_call_grant(job.repo, job.pr_number, target.head_sha, job.comment_id)
        approval = {"skill": skill, "comment_id": job.comment_id}
        results.append(update.resume_fix(target, deps, approval, guard.author(refs)))
    if results:
        return ";".join(results)
    # 제안 PR을 닫거나 머지한 뒤 바로 /fix를 달 수 있으니 기록을 GitHub과 맞춘 뒤 본다
    proposal.refresh(deps.store, deps.github, job.repo, job.pr_number)
    history, number = "", None
    if len(skills) == 1:
        history, number = proposal.blocked_by_history(
            deps.store, job.repo, job.pr_number, skills[0], target.head_sha
        )
    skill = skills[0] if len(skills) == 1 else ""
    body = no_pending_fix_message(target.head_sha, history, number, skill, typed=arg)
    deps.github.reply(job.repo, job.pr_number, body)
    return "no_pending_fix"
