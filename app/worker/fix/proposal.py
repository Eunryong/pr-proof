"""수정안 PR (docs/DESIGN.md 4.9): 묶음(기계적 수정, 모델 수정)마다 올리기, 상태 확인, 상황별 정리.

- 원본 PR에 새 커밋: 열린 수정안은 닫고 브랜치를 지운다 (superseded).
  새 커밋의 검사가 다시 만든다.
- 수정안 PR 머지 → 원본에 새 커밋: 새 head가 그 머지 커밋이면 같은 묶음의 수정안을 다시 만들지
  않는다 (루프 방지).
- 원본 PR 머지·닫힘: 열린 수정안을 닫고 브랜치를 지운다 (closed_with_pr).
- 사람이 수정안 PR을 닫음: rejected. 같은 커밋에는 다시 만들지 않는다. 그 수정안이 고친 위반은 거절
  기록(`records`)이 되어 다음 커밋에서 같은 줄 내용이면 수정 판단이 `skip`한다 (4.6).
"""

import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from app.shared import config, github_auth
from app.shared.job_pure import PRJob
from app.worker import records, repo
from app.worker.core.bundle_pure import FEEDBACK_BUNDLE, MECHANICAL_BUNDLE, MODEL_BUNDLE
from app.worker.core.markdown_pure import code_fence, inline_code
from app.worker.doc_rules_pure import Found, outcomes_by_rule
from app.worker.fix import proposal_pure
from app.worker.fix.feedback_fix_pure import Accepted, comment_lines, proposal_title
from app.worker.fix.fix_pure import MechanicalFix
from app.worker.fix.model_fix_pure import Explanation, ModelFix
from app.worker.fix.proposal_pure import ProposalResult
from app.worker.github import GitHub, GitHubError, ReviewComment
from app.worker.state import Store
from app.worker.testing.test_lines import generated_lines, runs_without_new_tests, status_lines

# v1의 lint 수정안 기록. 기계적 수정 묶음의 기록으로 본다 (머지로 생긴 커밋, 열린 수정안)
LEGACY_SKILLS: Final[Mapping[str, str]] = {"lint": MECHANICAL_BUNDLE}
MAX_LISTED_TARGETS: Final = 30
MAX_REVIEW_RULE_CHARS: Final = 300
MAX_REVIEW_OTHER_LINES: Final = 30
# gh가 전하는 GitHub의 2차 한도(짧은 시간에 너무 많이 만듦) 오류 글
SECONDARY_RATE_LIMIT: Final = "secondary rate limit"
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
    """같은 묶음의 수정안을 다시 만들면 안 되는 이유와 그 수정안 PR 번호. 괜찮으면 ("", None).

    - merged: 수정안을 머지해서 생긴 커밋. 다시 만들면 루프가 된다
    - rejected: 이 커밋의 수정안을 사람이 닫았다
    - open: 이 커밋의 수정안이 이미 열려 있다. 다시 만들지 않고 그 PR을 가리킨다
    """
    for p in store.proposals(repo_name, pr_number):
        if LEGACY_SKILLS.get(p.skill, p.skill) != skill:
            continue
        if p.state == "merged" and p.merge_commit == head:
            return "merged", p.number
        if p.state in ("rejected", "open") and p.head_sha == head:
            return p.state, p.number
    return "", None


def pipeline_secrets() -> list[str]:
    """제안 diff에 들어가면 안 되는 파이프라인 비밀값: 웹훅 비밀값과 GitHub 토큰(토큰 방식일 때).
    파일이 없거나 읽지 못하는 것은 뺀다."""
    found = []
    for name in (config.WEBHOOK_SECRET, github_auth.TOKEN_FILE):
        try:
            found.append(config.read_secret(name))
        except config.ConfigError:
            continue
    return found


def explanation_text(explanation: Explanation | None) -> str:
    """모델의 설명을 글 한 덩어리로. 비밀값 검사와 수정안 PR 본문의 코드 울타리에 쓴다.

    같은 (파일, 규칙, 이유)는 한 줄로 묶고 고친 줄 번호를 뒤에 적는다. 이유가 다르면 줄을 나눠
    모델이 쓴 글이 하나도 빠지지 않게 한다(비밀값 검사가 이 글을 본다).
    """
    if explanation is None:
        return ""
    at: dict[tuple[str, str, str], list[int]] = {}
    for c in explanation.changes:
        at.setdefault((c.file, c.rule, c.why), []).append(c.line)
    lines = [explanation.summary, ""]
    lines += [
        f"- {file} {rule}: {why} (줄 {_line_list(sorted(set(where)))})"
        for (file, rule, why), where in at.items()
    ]
    if explanation.untested:
        lines += ["", "테스트를 쓰지 못한 곳:"]
        lines += [f"- {n.file} {n.rule}: {n.why}" for n in explanation.untested]
    return "\n".join(lines).strip()


def _line_list(lines: Sequence[int]) -> str:
    shown = ", ".join(map(str, lines[:MAX_REVIEW_OTHER_LINES]))
    more = len(lines) - MAX_REVIEW_OTHER_LINES
    return f"{shown} 외 {more}곳" if more > 0 else shown


def fixed_lines(found: Found, ids: Sequence[str]) -> list[str]:
    """수정안이 고친 위반을 규칙별로. 규칙마다 원문과 출처(문서와 절, 또는 도구 설정 파일), 그 아래
    파일마다 고친 줄 번호 (4.9). 테스트 표시는 따로 묶는다(`test_section`)."""
    by_rule = outcomes_by_rule(found)
    wanted = set(ids)
    grouped: dict[str, dict[str, list[int]]] = {}
    for v in found.violations:
        if v.id in wanted:
            grouped.setdefault(v.rule_id, {}).setdefault(v.file, []).append(v.line)
    lines = []
    for rule_id, files in grouped.items():
        rule = by_rule[rule_id].rule
        source = rule.source.file
        if rule.source.section:
            source += f" > {rule.source.section}"
        lines.append(
            f"- `{rule_id}` {inline_code(rule.text, 500)} · 출처: {inline_code(source, 200)}"
        )
        where = [
            f"{inline_code(f, 200)} 줄 {_line_list(sorted(set(ns)))}" for f, ns in files.items()
        ]
        lines += [f"  - {w}" for w in where[:MAX_LISTED_TARGETS]]
        if len(where) > MAX_LISTED_TARGETS:
            lines.append(f"  - 외 파일 {len(where) - MAX_LISTED_TARGETS}개")
    return lines


def test_section(found: Found, ids: Sequence[str], fix: ModelFix | None = None) -> list[str]:
    """고친 위반마다 그 줄을 어느 테스트가 실행했는지와 만든 테스트 (4.9). 모델 수정은 출구 검사가
    정한 상태(`ModelFix.line_runs`), 기계적 수정은 있던 테스트의 실행 여부만 쓴다."""
    wanted = set(ids)
    violations = [v for v in found.violations if v.id in wanted]
    runs = (
        fix.line_runs if fix is not None and fix.line_runs else runs_without_new_tests(violations)
    )
    lines = ["", "**테스트** (파이프라인이 실행해 확인한 결과)", *status_lines(runs, violations)]
    if fix is not None:
        lines += generated_lines(fix.generated)
    return lines


def _unverified_line(count: int) -> list[str]:
    """모델이 찾은 위반은 고친 뒤 해소를 결정적으로 다시 확인하지 못한다 (4.8의 5번, 11.2 D2)."""
    if not count:
        return []
    return [
        f"- 모델 판단으로 찾은 위반 {count}건: 이 규칙은 도구로 재검사하지 못함. 그 줄이 바뀐 "
        "것까지만 확인했습니다. 해소 여부는 리뷰어가 확인해 주세요"
    ]


def model_fix_body(job: PRJob, fix: ModelFix, found: Found, skipped: int) -> str:
    """모델 수정안 PR의 본문 (4.9). 검증 절은 파이프라인이 쓰고, 모델의 글은 코드 울타리 안에만
    둔다."""
    check = fix.verification
    lines = [
        proposal_pure.PROPOSAL_MARKER,
        f"#{job.pr_number}의 커밋 `{job.head_sha[:7]}`에서 PR이 바꾼 줄의 문서 규칙 위반을 모델이 "
        "고친 수정안입니다.",
        "",
        f"**고친 위반** ({len(fix.targets)}건)",
        *fixed_lines(found, fix.targets),
    ]
    if skipped:
        lines += ["", f"파이프라인이 고치지 않은 위반 {skipped}건은 이 수정안에 넣지 않았습니다. "
                  f"직접 고쳐야 하며, 이유는 #{job.pr_number}의 결과 코멘트에 있습니다."
                  ]  # fmt: skip
    lines += ["", "**검증** (파이프라인이 확인한 결과)"]
    if check is not None:
        kept = [t for t in fix.generated if t.accepted]
        made = {t.file for t in kept}
        files = ", ".join(inline_code(f, 200) for f in fix.changed_files if f not in made)
        recheck = f"도구·검사기 {check.rules_rechecked}개"
        new_tests = (
            f"만든 테스트 파일 {len(kept)}개는 테스트 폴더의 새 파일. "
            if kept else ""
        )  # fmt: skip
        lines += [
            f"- 허용 범위: 위반이 있는 파일만 바뀜 ({files}). {new_tests}있던 테스트 파일, "
            "`.github/`, 새로 생긴 무시된 파일 없음",
            f"- 규칙 재검사: 대상 위반 {check.resolved}건 해소, 새 위반 없음 "
            f"({recheck}를 다시 돌림)",
            *_unverified_line(check.unverified),
            f"- 테스트: 있던 테스트 {check.tests}개가 수정 전후 같은 결과로 통과",
            *(
                [f"- 만든 테스트: {len(kept)}개가 고치기 전 코드와 고친 뒤 코드에서 모두 통과"]
                if kept
                else []
            ),  # fmt: skip
            f"- 크기: 바뀐 파일 {check.files}개, 바뀐 줄 {check.lines}줄 "
            f"(상한 {proposal_pure.MAX_FILES}개, {proposal_pure.MAX_CHANGED_LINES}줄. "
            "만든 테스트는 따로 셈)",
            "- 비밀값: 추가된 줄과 모델의 설명에서 찾지 못함",
            "- 설명 확인: 위반을 고친 곳마다 그 규칙의 설명이 있고, 바뀐 파일마다 설명이 있음. "
            "줄 리뷰 코멘트의 줄은 파이프라인이 diff에서 정함",
        ]
        lines += test_section(found, fix.targets, fix)
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
    """(파일, 규칙)마다 그 규칙으로 고친 첫 줄에 다는 "규칙, 이유" (4.9). 모델의 글은 코드 스팬
    안에만 둔다.

    이유는 (파일, 규칙)에 하나라 고친 곳마다 달면 같은 글이 되풀이되고, 큰 수정안에서는 리뷰 하나가
    GitHub의 2차 한도에 걸린다. 그래서 첫 줄에만 달고 나머지 줄은 그 코멘트에 적는다. (file, line)은
    검증(4.8의 4번)이 대상 위반의 줄을 바꾼 덩어리에서 채운 값이라 이 수정의 diff에 있다.
    """
    if fix.explanation is None:
        return []
    texts = {rule_id: o.rule.text for rule_id, o in outcomes_by_rule(found).items()}
    lines: dict[tuple[str, str], list[int]] = {}
    why: dict[tuple[str, str], str] = {}
    for c in fix.explanation.changes:
        lines.setdefault((c.file, c.rule), []).append(c.line)
        why.setdefault((c.file, c.rule), c.why)
    comments = []
    for (file, rule), at in lines.items():
        first, rest = min(at), sorted(set(at) - {min(at)})
        body = (
            f"규칙 {inline_code(rule, 100)} "
            f"{inline_code(texts.get(rule, ''), MAX_REVIEW_RULE_CHARS)}\n\n"
            f"이유 (모델의 설명): {inline_code(why[(file, rule)] or '이유 없음', 300)}"
        )
        if rest:
            body += f"\n\n이 파일에서 같은 규칙으로 고친 다른 줄: {_line_list(rest)}"
        comments.append(ReviewComment(file, first, body))
    return comments


def post_review(
    github: GitHub, repo_name: str, number: int, comments: Sequence[ReviewComment], patch: str,
    secrets: Sequence[str],
) -> None:  # fmt: skip
    """줄 리뷰 코멘트를 리뷰 하나로 단다. 같은 패치의 리뷰가 이미 있으면 달지 않는다 (5.6).

    전달을 다시 시도해도 두 번 달리지 않는다. GitHub이 줄을 받지 않거나(422) 2차 한도로 막으면
    수정안 PR은 그대로 두고 리뷰만 빠진다. 리뷰는 수정안 PR에 덧붙이는 것이라, 리뷰 때문에 결과
    코멘트와 기록까지 멈추지 않게 한다.
    """
    if not comments:
        return
    marker = proposal_pure.review_marker(patch)
    if github.has_review(repo_name, number, marker):
        return
    if proposal_pure.find_secret_in_text("\n".join(c.body for c in comments), secrets) is not None:
        log.warning("줄 리뷰 코멘트를 달지 않음 %s#%s: 비밀값", repo_name, number)
        return
    try:
        github.create_review(repo_name, number, f"{marker}\n{REVIEW_BODY}", comments)
    except GitHubError as e:
        if "422" not in str(e) and SECONDARY_RATE_LIMIT not in str(e):
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
    blocker = proposal_pure.push_blocker(patch, fix.changed_files, secrets)
    if blocker is not None:
        log.warning("수정안 PR을 만들지 않음 %s#%s: %s", job.repo, job.pr_number, blocker)
        return ProposalResult(reason=blocker)
    # 모델의 설명도 푸시하기 전에 한 번 더 본다 (4.8의 3번 "전달 직전에 한 번 더")
    if proposal_pure.find_secret_in_text(explanation_text(fix.explanation), secrets) is not None:
        log.warning("수정안 PR을 만들지 않음 %s#%s: 모델의 설명", job.repo, job.pr_number)
        return ProposalResult(reason=f"{proposal_pure.SECRET_BLOCKED} (모델의 설명)")

    count = len(fix.targets)
    branch = proposal_pure.branch_name(job.pr_number, MODEL_BUNDLE)
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


def feedback_fix_body(job: PRJob, fix: ModelFix, found: Found, accepted: Sequence[Accepted]) -> str:
    """코멘트 수정안 PR의 본문 (4.11). 검증 절은 파이프라인이 쓰고, 사람과 모델의 글은 코드 스팬과
    코드 울타리 안에만 둔다. 코멘트에는 검사기가 없어 "대상 위반 해소" 대신 그 파일이 바뀐 것만
    확인했다고 적는다 (4.8의 5번이 약해진 자리)."""
    check = fix.verification
    lines = [
        proposal_pure.PROPOSAL_MARKER,
        f"#{job.pr_number}의 커밋 `{job.head_sha[:7]}`에 달린 리뷰 코멘트 중 파이프라인이 받아들인 "
        "것을 모델이 고친 수정안입니다.",
        "",
        f"**반영한 코멘트** ({len(accepted)}건)",
        *comment_lines(accepted, fix),
        "",
        "**검증** (파이프라인이 확인한 결과)",
    ]
    if check is not None:
        kept = [t for t in fix.generated if t.accepted]
        made = {t.file for t in kept}
        files = ", ".join(inline_code(f, 200) for f in fix.changed_files if f not in made)
        new_tests = f"만든 테스트 파일 {len(kept)}개는 테스트 폴더의 새 파일. " if kept else ""
        recheck = (
            f"이 커밋의 규칙 검사가 쓴 도구·검사기 {check.rules_rechecked}개를 다시 돌려 새 위반 "
            "없음"
            if check.rules_rechecked
            else "이 커밋의 규칙 검사 결과가 없어 도구·검사기를 다시 돌리지 않음"
        )
        lines += [
            f"- 허용 범위: 판단이 정한 파일만 바뀜 ({files}). {new_tests}있던 테스트 파일, "
            "`.github/`, 새로 생긴 무시된 파일 없음",
            f"- 코멘트 반영: 검사기가 없어 그 파일이 바뀐 것까지만 확인했습니다 "
            f"({check.resolved}건). 반영 여부는 리뷰어가 확인해 주세요",
            f"- 규칙 재검사: {recheck}",
            f"- 테스트: 있던 테스트 {check.tests}개가 수정 전후 같은 결과로 통과",
            *([f"- 만든 테스트: {len(kept)}개가 고치기 전 코드와 고친 뒤 코드에서 모두 통과"]
              if kept else []),
            f"- 크기: 바뀐 파일 {check.files}개, 바뀐 줄 {check.lines}줄 "
            f"(상한 {proposal_pure.MAX_FILES}개, {proposal_pure.MAX_CHANGED_LINES}줄. "
            "만든 테스트는 따로 셈)",
            "- 비밀값: 추가된 줄과 모델의 설명에서 찾지 못함",
        ]  # fmt: skip
        lines += test_section(found, fix.targets, fix)
    text = explanation_text(fix.explanation)
    if text:
        fence = code_fence(text)
        lines += ["", "**모델의 설명** (모델이 쓴 글입니다)", "", f"{fence}text", text, fence]
    models = ", ".join(inline_code(m, 80) for m in fix.models) or "저장된 결과"
    lines += [
        "",
        f"모델: {models} · 작성 회차 {fix.attempts}",
        "",
        f"머지하면 #{job.pr_number}의 브랜치에 들어가고, 반영한 줄 코멘트의 스레드를 파이프라인이 "
        "해결됨으로 바꿉니다. 모델이 쓴 코드이니 리뷰한 뒤 머지해 주세요. 원하지 않으면 닫아 "
        "주세요.",
    ]
    return "\n".join(lines) + "\n"


def open_feedback_proposal(
    job: PRJob,
    worktree: Path,
    store: Store,
    github: GitHub,
    fix: ModelFix,
    found: Found,
    accepted: Sequence[Accepted],
    *,
    patch: str,
    secrets: Sequence[str] | None = None,
) -> ProposalResult:
    """검증을 통과한 코멘트 수정안을 수정안 PR로 올린다 (4.11). `open_model_proposal`의 짝이고 막는
    규칙(포크, 새 커밋, 비밀값)과 다시 불러도 중복되지 않는 것이 같다. 브랜치는
    pipeline/pr-<n>-fix-feedback, base는 원래 PR의 브랜치다."""
    refs = github.pr_refs(job.repo, job.pr_number)
    if refs.head_repo != job.repo:
        return ProposalResult(reason=FORK_NO_PR)
    if refs.head_sha != job.head_sha:
        return ProposalResult(reason="그 사이 새 커밋이 올라옴")
    secrets = list(pipeline_secrets() if secrets is None else secrets)
    blocker = proposal_pure.push_blocker(patch, fix.changed_files, secrets)
    if blocker is not None:
        log.warning("코멘트 수정안 PR을 만들지 않음 %s#%s: %s", job.repo, job.pr_number, blocker)
        return ProposalResult(reason=blocker)
    body = feedback_fix_body(job, fix, found, accepted)
    if proposal_pure.find_secret_in_text(body, secrets) is not None:
        log.warning("코멘트 수정안 PR을 만들지 않음 %s#%s: 본문", job.repo, job.pr_number)
        return ProposalResult(reason=f"{proposal_pure.SECRET_BLOCKED} (수정안 본문)")
    branch = proposal_pure.branch_name(job.pr_number, FEEDBACK_BUNDLE)
    message = (
        f"[pipeline] feedback: #{job.pr_number} 리뷰 코멘트 {len(accepted)}건 반영\n\n"
        f"원본 커밋 {job.head_sha}"
    )
    repo.commit_and_push(worktree, fix.changed_files, message, branch, github.identity())
    title = proposal_title(job.pr_number, len(accepted))
    existing = github.open_pr_for_branch(job.repo, branch)
    if existing is not None:
        github.update_pr(job.repo, existing, title, body)
        number = existing
    else:
        number = github.create_pr(job.repo, refs.head_ref, branch, title, body)
    store.record_proposal(
        job.repo, job.pr_number, FEEDBACK_BUNDLE, job.head_sha, branch, number,
        attempt_id=fix.attempt_id,
    )  # fmt: skip
    return ProposalResult(
        number=number,
        url=f"https://github.com/{job.repo}/pull/{number}",
        updated=existing is not None,
    )


def mechanical_fix_body(job: PRJob, fix: MechanicalFix, found: Found, tool: str) -> str:
    """기계적 수정안 PR의 본문 (4.9). 모든 글은 파이프라인이 쓴다. tool은 쓴 도구와 버전이다."""
    fixed = [v for v in found.violations if v.id in set(fix.fixed)]
    lines = [
        proposal_pure.PROPOSAL_MARKER,
        f"#{job.pr_number}의 커밋 `{job.head_sha[:7]}`에서 PR이 바꾼 줄의 규칙 위반을 도구의 자동 "
        "수정으로 고친 수정안입니다.",
        "",
        f"**고친 위반** ({len(fix.fixed)}건)",
        *fixed_lines(found, fix.fixed),
    ]
    rewrote = sorted(proposal_pure.BLOCK_REWRITE_CODES & {v.code for v in fixed})
    if rewrote:
        lines += ["", f"{', '.join(f'`{c}`' for c in rewrote)}(import 정렬)는 import 블록 전체를 "
                  "다시 정렬했습니다. 블록 안의 기존 줄 순서가 바뀌었을 수 있습니다."]  # fmt: skip
    if fix.formatted_files:
        lines += [
            "",
            f"`ruff format`을 고친 파일 {len(fix.formatted_files)}개의 전체에 적용했습니다. "
            "base 브랜치가 이미 포맷돼 있어 기존 줄은 대부분 그대로입니다.",
        ]
    if fix.remaining:
        lines += ["", f"자동으로 고치지 못한 위반 {len(fix.remaining)}건은 그대로 남습니다."]
    files = ", ".join(inline_code(f, 200) for f in fix.changed_files)
    lines += [
        "",
        "**검증** (파이프라인이 확인한 결과)",
        f"- 허용 범위: 고친 파일의 내용만 바뀜 ({files})",
        "- 규칙 재검사: 새 위반 없음 (도구·검사기로 확인하는 규칙을 모두 다시 돌림)",
        f"- 테스트: 고친 뒤 {fix.test_line} (수정 전과 같은 결과)",
        "- 비밀값: 추가된 줄에서 찾지 못함",
        *test_section(found, fix.fixed),
        "",
        f"도구: {tool}. 도구의 안전한 수정만 썼고, 모델은 쓰지 않았습니다.",
        "",
        f"머지하면 #{job.pr_number}의 브랜치에 들어갑니다. 원하지 않으면 닫아 주세요. "
        "같은 커밋에는 다시 만들지 않습니다.",
    ]
    return "\n".join(lines) + "\n"


def open_mechanical_proposal(
    job: PRJob,
    worktree: Path,
    store: Store,
    github: GitHub,
    fix: MechanicalFix,
    found: Found,
    *,
    patch: str,
    tool: str,
    secrets: Sequence[str] | None = None,
) -> ProposalResult:
    """검증을 통과한 기계적 수정안을 수정안 PR로 올린다 (4.9). 올리지 못하면 이유를 돌려준다.

    worktree에는 저장한 패치가 적용돼 있다(`model_fix.reapply`). 다시 불러도 같은 브랜치에 같은
    내용을 강제 푸시하고 열린 수정안 PR은 제목·본문만 고치므로 중복되지 않는다 (5.6).
    """
    refs = github.pr_refs(job.repo, job.pr_number)
    if refs.head_repo != job.repo:
        return ProposalResult(reason=FORK_NO_PR)
    if refs.head_sha != job.head_sha:
        return ProposalResult(reason="그 사이 새 커밋이 올라옴")
    blocker = proposal_pure.push_blocker(
        patch, fix.changed_files, pipeline_secrets() if secrets is None else secrets
    )
    if blocker is not None:
        log.warning("수정안 PR을 만들지 않음 %s#%s: %s", job.repo, job.pr_number, blocker)
        return ProposalResult(reason=blocker)
    count = len(fix.fixed)
    branch = proposal_pure.branch_name(job.pr_number, MECHANICAL_BUNDLE)
    message = (
        f"[pipeline] mechanical: #{job.pr_number} 위반 {count}건 수정\n\n원본 커밋 {job.head_sha}"
    )
    repo.commit_and_push(worktree, fix.changed_files, message, branch, github.identity())
    what = f"위반 {count}건 수정" if count else "포맷 정리"
    title = f"[pipeline] #{job.pr_number} {what} (기계적 수정)"
    body = mechanical_fix_body(job, fix, found, tool)
    existing = github.open_pr_for_branch(job.repo, branch)
    if existing is not None:
        github.update_pr(job.repo, existing, title, body)
        number = existing
    else:
        number = github.create_pr(job.repo, refs.head_ref, branch, title, body)
    # 머지·닫힘의 기록(4.10)은 이 시도의 대상 위반에서 만든다. 거절되면 다음 커밋에서 같은 줄을 skip
    store.record_proposal(
        job.repo, job.pr_number, MECHANICAL_BUNDLE, job.head_sha, branch, number,
        attempt_id=fix.attempt_id,
    )  # fmt: skip
    return ProposalResult(
        number=number,
        url=f"https://github.com/{job.repo}/pull/{number}",
        updated=existing is not None,
    )
