"""제안 PR (docs/DESIGN.md F10): 올리기, 상태 확인, 상황별 정리.

- 원본 PR에 새 커밋: 열린 제안은 닫고 브랜치를 지운다 (superseded).
  새 커밋의 검사가 다시 제안한다.
- 제안 PR 머지 → 원본에 새 커밋: 새 head가 그 머지 커밋이면 같은 스킬의 제안을 다시 만들지
  않는다 (루프 방지).
- 원본 PR 머지·닫힘: 열린 제안을 닫고 브랜치를 지운다 (closed_with_pr).
- 사람이 제안 PR을 닫음: rejected. 같은 커밋에는 다시 제안하지 않는다. 다음 커밋에서도 그 제안이
  고친 위반이 같은 줄 내용 그대로면 수정 대상에서 뺀다 (app/models/rejection.py).
"""

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from app import config
from app.models import proposal as rules
from app.models import rejection
from app.models.doc_rules import Found
from app.models.finding import SkillResult
from app.models.fix import FixOutcome
from app.models.job import PRJob
from app.models.model_fix import Explanation, ModelFix
from app.models.proposal import ProposalResult
from app.worker import records, repo
from app.worker.github import GitHub, GitHubError, ReviewComment
from app.worker.markdown import inline_code
from app.worker.report import code_fence, format_test_line
from app.worker.state import Store

# 모델 수정 묶음 (DESIGN.md 4.9, 3.1의 C7). 브랜치는 pipeline/pr-<n>-fix-model
MODEL_BUNDLE: Final = "model"
MAX_LISTED_TARGETS: Final = 30
MAX_REVIEW_RULE_CHARS: Final = 300
REVIEW_BODY: Final = (
    "모델이 바꾼 줄마다 규칙과 이유를 남겼습니다. 이유는 모델이 쓴 글이니 diff와 같이 확인해 "
    "주세요."
)
FORK_NO_PR: Final = "포크에서 온 PR이라 수정안 PR을 열 수 없음 (수정 내용은 결과 코멘트에)"

log = logging.getLogger(__name__)


def refresh(store: Store, github: GitHub, repo_name: str, pr_number: int) -> None:
    """열린 제안의 상태를 GitHub에서 확인해 기록한다. 머지 여부는 커밋 메시지로 판단하지 않는다.

    문서 규칙 수정안이 머지됐거나 닫혔으면 채택·거절 기록을 남긴다 (4.10).
    """
    for p in store.proposals(repo_name, pr_number, state="open"):
        status = github.pull_status(repo_name, p.number)
        if status.merged:
            store.set_proposal_state(p.id, "merged", merge_commit=status.merge_commit)
        elif not status.open:
            store.set_proposal_state(p.id, "rejected")
    records.after_proposals(store, repo_name, pr_number, pipeline_secrets())


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


def blocked_by_history(
    store: Store, repo_name: str, pr_number: int, skill: str, head: str
) -> tuple[str, int | None]:
    """같은 스킬의 제안을 다시 만들면 안 되는 이유와 그 제안 PR 번호. 괜찮으면 ("", None).

    - merged: 제안을 머지해서 생긴 커밋. 다시 제안하면 루프가 된다
    - rejected: 이 커밋의 제안을 사람이 닫았다
    - open: 이 커밋의 제안이 이미 열려 있다. 다시 권하지 않고 그 PR을 가리킨다
    """
    for p in store.proposals(repo_name, pr_number):
        if p.skill != skill:
            continue
        if p.state == "merged" and p.merge_commit == head:
            return "merged", p.number
        if p.state in ("rejected", "open") and p.head_sha == head:
            return p.state, p.number
    return "", None


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
    # AI가 쓴 변경 설명도 같은 검사를 거친다. 그래프가 이미 봤지만 푸시하기 전에 한 번 더 본다
    if rules.find_secret_in_text(outcome.explanation, secrets) is not None:
        log.warning("제안 PR을 만들지 않음 %s#%s: AI 변경 설명", job.repo, job.pr_number)
        return ProposalResult(reason=f"{rules.SECRET_BLOCKED} (AI 변경 설명)")

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
    if skill == "lint":
        body = rules.body(
            job.pr_number, skill, job.head_sha, codes, before.source, tests,
            len(outcome.formatted_files),
        )  # fmt: skip
    else:
        body = rules.ai_body(
            job.pr_number, skill, job.head_sha, codes, before.source, tests, outcome.models,
            outcome.explanation, code_fence(outcome.explanation),
        )  # fmt: skip
    existing = github.open_pr_for_branch(job.repo, branch)
    if existing is not None:
        github.update_pr(job.repo, existing, title, body)
        number = existing
    else:
        number = github.create_pr(job.repo, refs.head_ref, branch, title, body)
    # 사람이 이 제안을 닫으면 다음 커밋에서 같은 위반을 다시 올리지 않으려고 고친 위반을 남긴다
    targets = rejection.fixed_keys(before.findings, outcome.remaining)
    store.record_proposal(job.repo, job.pr_number, skill, job.head_sha, branch, number, targets)
    return ProposalResult(
        number=number,
        url=f"https://github.com/{job.repo}/pull/{number}",
        updated=existing is not None,
    )


def explanation_text(explanation: Explanation | None) -> str:
    """모델의 설명을 글 한 덩어리로. 비밀값 검사와 수정안 PR 본문의 코드 울타리에 쓴다."""
    if explanation is None:
        return ""
    lines = [explanation.summary, ""]
    lines += [f"- {c.file}:{c.line} {c.rule}: {c.why}" for c in explanation.changes]
    return "\n".join(lines).strip()


def model_fix_body(job: PRJob, fix: ModelFix, found: Found, skipped: int) -> str:
    """모델 수정안 PR의 본문 (4.9). 검증 절은 파이프라인이 쓰고, 모델의 글은 코드 울타리 안에만
    둔다."""
    rules_by_id = {o.rule.id: o.rule for o in found.outcomes}
    targets = [v for v in found.violations if v.id in set(fix.targets)]
    check = fix.verification
    lines = [
        rules.PROPOSAL_MARKER,
        f"#{job.pr_number}의 커밋 `{job.head_sha[:7]}`에서 PR이 바꾼 줄의 문서 규칙 위반을 모델이 "
        "고친 수정안입니다.",
        "",
        f"**고친 위반** ({len(targets)}건)",
    ]
    by_rule: dict[str, list[str]] = {}
    for v in targets:
        by_rule.setdefault(v.rule_id, []).append(inline_code(f"{v.file}:{v.line}", 200))
    for rule_id, where in by_rule.items():
        rule = rules_by_id[rule_id]
        source = rule.source.file
        if rule.source.section:
            source += f" > {rule.source.section}"
        lines.append(
            f"- `{rule_id}` {inline_code(rule.text, 500)} · 출처: {inline_code(source, 200)}"
        )
        lines += [f"  - {w}" for w in where[:MAX_LISTED_TARGETS]]
        if len(where) > MAX_LISTED_TARGETS:
            lines.append(f"  - 외 {len(where) - MAX_LISTED_TARGETS}건")
    if skipped:
        lines += ["", f"수정 판단이 skip한 위반 {skipped}건은 이 수정안에 넣지 않았습니다. 이유는 "
                  f"#{job.pr_number}의 결과 코멘트에 있습니다."]  # fmt: skip
    lines += ["", "**검증** (파이프라인이 확인한 결과)"]
    if check is not None:
        files = ", ".join(inline_code(f, 200) for f in fix.changed_files)
        recheck = f"검사기 {check.rules_rechecked}개" + (", ruff" if check.lint_rechecked else "")
        lines += [
            f"- 허용 범위: 위반이 있는 파일만 바뀜 ({files}). 테스트 파일, `.github/`, 새로 생긴 "
            "무시된 파일 없음",
            f"- 규칙 재검사: 대상 위반 {check.resolved}건 해소, 새 위반 없음 "
            f"({recheck}를 다시 돌림)",
            f"- 테스트: 수정 전후 테스트 {check.tests}개가 같은 결과로 통과",
            f"- 크기: 바뀐 파일 {check.files}개, 바뀐 줄 {check.lines}줄 "
            f"(상한 {rules.MAX_FILES}개, {rules.MAX_CHANGED_LINES}줄)",
            "- 비밀값: 추가된 줄과 모델의 설명에서 찾지 못함",
            "- 설명 확인: 모델의 설명이 가리키는 줄이 모두 이 수정에서 바뀐 줄이고, 바뀐 곳마다 "
            "설명이 있음",
        ]
    text = explanation_text(fix.explanation)
    if text:
        fence = code_fence(text)
        lines += ["", "**모델의 설명** (모델이 쓴 글입니다)", "", f"{fence}text", text, fence]
    models = ", ".join(inline_code(m, 80) for m in fix.models) or "저장된 결과"
    lines += [
        "",
        f"모델: {models} · 작성 회차 {fix.attempts}",
        "",
        f"머지하면 #{job.pr_number}의 브랜치에 들어갑니다. 모델이 쓴 코드이니 리뷰한 뒤 머지해 "
        "주세요. 원하지 않으면 닫아 주세요. 같은 커밋에는 다시 제안하지 않습니다.",
    ]
    return "\n".join(lines) + "\n"


def review_comments(fix: ModelFix, found: Found) -> list[ReviewComment]:
    """설명 JSON의 changes마다 그 줄에 다는 "규칙, 이유" (4.9). 모델의 글은 코드 스팬 안에만 둔다.

    (file, line)은 검증(4.8의 4번)이 이 수정의 바뀐 줄을 가리키는지 확인한 값이다.
    """
    if fix.explanation is None:
        return []
    texts = {o.rule.id: o.rule.text for o in found.outcomes}
    return [
        ReviewComment(
            c.file, c.line,
            f"규칙 {inline_code(c.rule, 100)} "
            f"{inline_code(texts.get(c.rule, ''), MAX_REVIEW_RULE_CHARS)}\n\n"
            f"이유 (모델의 설명): {inline_code(c.why or '이유 없음', 300)}",
        )
        for c in fix.explanation.changes
    ]  # fmt: skip


def post_review(
    github: GitHub, repo_name: str, number: int, comments: Sequence[ReviewComment], patch: str,
    secrets: Sequence[str],
) -> None:  # fmt: skip
    """줄 리뷰 코멘트를 리뷰 하나로 단다. 같은 패치의 리뷰가 이미 있으면 달지 않는다 (5.6).

    전달을 다시 시도해도 두 번 달리지 않는다. GitHub이 줄을 받지 않으면(422) 수정안 PR은 그대로 두고
    리뷰만 빠진다.
    """
    if not comments:
        return
    marker = rules.review_marker(patch)
    if github.has_review(repo_name, number, marker):
        return
    if rules.find_secret_in_text("\n".join(c.body for c in comments), secrets) is not None:
        log.warning("줄 리뷰 코멘트를 달지 않음 %s#%s: 비밀값", repo_name, number)
        return
    try:
        github.create_review(repo_name, number, f"{marker}\n{REVIEW_BODY}", comments)
    except GitHubError as e:
        if "422" not in str(e):
            raise
        log.warning("줄 리뷰 코멘트를 GitHub이 받지 않음 %s#%s: %s", repo_name, number, e)


def open_model_proposal(
    job: PRJob,
    worktree: Path,
    store: Store,
    github: GitHub,
    fix: ModelFix,
    found: Found,
    *,
    patch: str,
    skipped: int,
    secrets: Sequence[str] | None = None,
) -> ProposalResult:
    """검증을 통과한 모델 수정안을 수정안 PR로 올린다 (4.9). 올리지 못하면 이유를 돌려준다.

    worktree에는 저장한 패치(patch)가 적용돼 있다(`model_fix.reapply`). 다시 불러도 같은 브랜치에
    같은 내용을 강제 푸시하고 열린 수정안 PR은 제목·본문만 고치므로 중복되지 않는다 (5.6).
    포크에서 온 PR에는 열지 않는다 (8.5). 그때는 결과 코멘트에 수정 내용만 보인다.
    """
    refs = github.pr_refs(job.repo, job.pr_number)
    if refs.head_repo != job.repo:
        return ProposalResult(reason=FORK_NO_PR)
    if refs.head_sha != job.head_sha:
        return ProposalResult(reason="그 사이 새 커밋이 올라옴")
    secrets = list(pipeline_secrets() if secrets is None else secrets)
    blocker = rules.push_blocker(patch, fix.changed_files, secrets)
    if blocker is not None:
        log.warning("수정안 PR을 만들지 않음 %s#%s: %s", job.repo, job.pr_number, blocker)
        return ProposalResult(reason=blocker)
    # 모델의 설명도 푸시하기 전에 한 번 더 본다 (4.8의 3번 "전달 직전에 한 번 더")
    if rules.find_secret_in_text(explanation_text(fix.explanation), secrets) is not None:
        log.warning("수정안 PR을 만들지 않음 %s#%s: 모델의 설명", job.repo, job.pr_number)
        return ProposalResult(reason=f"{rules.SECRET_BLOCKED} (모델의 설명)")

    count = len(fix.targets)
    branch = rules.branch_name(job.pr_number, MODEL_BUNDLE)
    message = (
        f"[pipeline] model: #{job.pr_number} 문서 규칙 위반 {count}건 수정\n\n"
        f"원본 커밋 {job.head_sha}"
    )
    repo.commit_and_push(worktree, fix.changed_files, message, branch, github.identity())
    title = f"[pipeline] #{job.pr_number} 문서 규칙 위반 {count}건 수정 (모델)"
    body = model_fix_body(job, fix, found, skipped)
    existing = github.open_pr_for_branch(job.repo, branch)
    if existing is not None:
        github.update_pr(job.repo, existing, title, body)
        number = existing
    else:
        number = github.create_pr(job.repo, refs.head_ref, branch, title, body)
    # 머지·닫힘의 기록(4.10)은 이 시도의 대상 위반에서 만든다. 거절되면 다음 커밋에서 같은 줄을 skip
    store.record_proposal(
        job.repo, job.pr_number, MODEL_BUNDLE, job.head_sha, branch, number,
        attempt_id=fix.attempt_id,
    )  # fmt: skip
    post_review(github, job.repo, number, review_comments(fix, found), patch, secrets)
    return ProposalResult(
        number=number,
        url=f"https://github.com/{job.repo}/pull/{number}",
        updated=existing is not None,
    )
