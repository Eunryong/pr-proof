"""제안 PR (docs/DESIGN.md F10): 올리기, 상태 확인, 상황별 정리.

- 원본 PR에 새 커밋: 열린 제안은 닫고 브랜치를 지운다 (superseded).
  새 커밋의 검사가 다시 제안한다.
- 제안 PR 머지 → 원본에 새 커밋: 새 head가 그 머지 커밋이면 같은 스킬의 제안을 다시 만들지
  않는다 (루프 방지).
- 원본 PR 머지·닫힘: 열린 제안을 닫고 브랜치를 지운다 (closed_with_pr).
- 사람이 제안 PR을 닫음: rejected. 같은 커밋에는 다시 제안하지 않는다.
"""

import logging
from pathlib import Path

from app import config
from app.models import proposal as rules
from app.models.finding import SkillResult
from app.models.fix import FixOutcome
from app.models.job import PRJob
from app.models.proposal import ProposalResult
from app.worker import repo
from app.worker.github import GitHub
from app.worker.report import format_test_line
from app.worker.state import Store

log = logging.getLogger(__name__)


def refresh(store: Store, github: GitHub, repo_name: str, pr_number: int) -> None:
    """열린 제안의 상태를 GitHub에서 확인해 기록한다. 머지 여부는 커밋 메시지로 판단하지 않는다."""
    for p in store.proposals(repo_name, pr_number, state="open"):
        status = github.pull_status(repo_name, p.number)
        if status.merged:
            store.set_proposal_state(p.id, "merged", merge_commit=status.merge_commit)
        elif not status.open:
            store.set_proposal_state(p.id, "rejected")


def supersede(store: Store, github: GitHub, repo_name: str, pr_number: int, head_sha: str) -> None:
    """원본 PR에 새 커밋이 오면 이전 커밋의 열린 제안을 닫는다."""
    for p in store.proposals(repo_name, pr_number, state="open"):
        if p.head_sha == head_sha:
            continue
        github.close_pr(
            repo_name, p.number,
            f"#{pr_number}에 새 커밋(`{head_sha[:7]}`)이 올라와 이 제안을 닫습니다. "
            "새 커밋에 고칠 위반이 있으면 다시 제안합니다.",
        )  # fmt: skip
        github.delete_branch(repo_name, p.branch)
        store.set_proposal_state(p.id, "superseded")


def close_all(store: Store, github: GitHub, repo_name: str, pr_number: int) -> int:
    """원본 PR이 닫히면 남은 제안을 닫고 브랜치를 지운다."""
    refresh(store, github, repo_name, pr_number)
    closed = 0
    for p in store.proposals(repo_name, pr_number, state="open"):
        github.close_pr(repo_name, p.number, f"#{pr_number}이 닫혀 이 제안도 닫습니다.")
        github.delete_branch(repo_name, p.branch)
        store.set_proposal_state(p.id, "closed_with_pr")
        closed += 1
    return closed


def blocked_by_history(store: Store, repo_name: str, pr_number: int, skill: str, head: str) -> str:
    """같은 스킬의 제안을 다시 만들면 안 되는 이유. 괜찮으면 빈 문자열."""
    for p in store.proposals(repo_name, pr_number):
        if p.skill != skill:
            continue
        if p.state == "merged" and p.merge_commit == head:
            return "merged"  # 제안을 머지해서 생긴 커밋. 다시 제안하면 루프가 된다
        if p.state == "rejected" and p.head_sha == head:
            return "rejected"
    return ""


def pipeline_secrets() -> list[str]:
    """제안 diff에 들어가면 안 되는 파이프라인 비밀값. 파일이 없으면 빈 목록."""
    try:
        return [config.read_secret(config.WEBHOOK_SECRET)]
    except config.ConfigError:
        return []


def open_proposal(
    job: PRJob,
    worktree: Path,
    store: Store,
    github: GitHub,
    outcome: FixOutcome,
    before: SkillResult,
    secrets: list[str] | None = None,
) -> ProposalResult:
    """검증을 통과한 수정안을 제안 PR로 올린다. 올리지 못하면 이유를 돌려준다."""
    refs = github.pr_refs(job.repo, job.pr_number)
    if refs.head_repo != job.repo:
        return ProposalResult(reason="포크에서 온 PR이라 제안 PR을 열 수 없음 (수정 내용은 위에)")
    if refs.head_sha != job.head_sha:
        return ProposalResult(reason="그 사이 새 커밋이 올라옴")
    patch = outcome.patch_path.read_text() if outcome.patch_path else ""
    secrets = pipeline_secrets() if secrets is None else secrets
    blocker = rules.push_blocker(patch, outcome.changed_files, secrets)
    if blocker is not None:
        log.warning("제안 PR을 만들지 않음 %s#%s: %s", job.repo, job.pr_number, blocker)
        return ProposalResult(reason=blocker)

    skill = outcome.skill
    branch = rules.branch_name(job.pr_number, skill)
    message = (
        f"[pipeline] {skill}: #{job.pr_number} 위반 {outcome.fixed}건 수정\n\n"
        f"원본 커밋 {job.head_sha}"
    )
    repo.commit_and_push(worktree, outcome.changed_files, message, branch, github.identity())
    codes = rules.fixed_codes(before.findings, outcome.remaining)
    tests = format_test_line(outcome.test_run) if outcome.test_run else "확인 안 함"
    title = rules.title(job.pr_number, skill, outcome.fixed)
    body = rules.body(
        job.pr_number, skill, job.head_sha, codes, before.source, tests,
        len(outcome.formatted_files),
    )  # fmt: skip
    existing = github.open_pr_for_branch(job.repo, branch)
    if existing is not None:
        github.update_pr(job.repo, existing, title, body)
        number = existing
    else:
        number = github.create_pr(job.repo, refs.head_ref, branch, title, body)
    store.record_proposal(job.repo, job.pr_number, skill, job.head_sha, branch, number)
    return ProposalResult(
        number=number,
        url=f"https://github.com/{job.repo}/pull/{number}",
        updated=existing is not None,
    )
