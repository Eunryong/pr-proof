"""기계적 수정 묶음 (docs/DESIGN.md 4.7 기계적 수정, 4.8, 3.1의 C7): 도구 자신의 수정을 적용하고
다른 수정과 같이 결정적으로 검증한다. 모델을 부르지 않는다.

- 수정 판단(4.6)이 `fix`·`mechanical`로 정한 위반만 고친다. ruff의 안전한 수정 중 그 위반의 편집만
  적용한다. `ruff check --fix`는 PR이 건드리지 않은 줄까지 고치므로 쓰지 않는다. 편집이 겹치면 한
  회차에 하나만 적용하고 ruff를 다시 돌려 최대 5회 반복한다.
- `ruff format`은 base가 `ruff format --check`를 통과하는 저장소에서만, 고친 파일에 적용한다.
- 검증은 4.8의 순서다: 허용 범위(고친 파일의 수정만), 비밀값, 규칙 재검사(도구·검사기로 확인하는
  모든 규칙, 새 위반 없음), 테스트 결과 동일, 크기 상한. 통과한 패치는 `fix_attempts`에 저장하고
  전달은 그 패치만 올린다.
"""

import json
import logging
from collections.abc import Callable, Collection, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Final

from app.shared.db_pure import FixAttempt
from app.shared.job_pure import PRJob
from app.worker import records_pure, repo
from app.worker.checker_authoring_pure import Checked, RuleCheck
from app.worker.core.bundle_pure import MECHANICAL_BUNDLE
from app.worker.core.diff_pure import added_lines_except_moved, added_texts, new_files
from app.worker.core.text_edit_pure import TextEdit, apply_edits
from app.worker.core.violation_pure import Violation
from app.worker.doc_rules_pure import Found
from app.worker.fix import model_fix_pure, scope
from app.worker.fix.fix_decision_pure import FixDecision
from app.worker.fix.fix_pure import MechanicalFix, MechanicalStatus, same_test_result
from app.worker.fix.model_fix import file_hashes
from app.worker.fix.model_fix_pure import Picked
from app.worker.fix.proposal_pure import SECRET_BLOCKED, changed_line_count, find_secret
from app.worker.fix.scope_pure import MECHANICAL_POLICY, is_pipeline_output, parse_porcelain
from app.worker.lang import python
from app.worker.rules import detect
from app.worker.rules.checkers import FormatsFiles
from app.worker.state import Store
from app.worker.test_lines import format_test_line
from app.worker.test_run_pure import PytestRun

log = logging.getLogger(__name__)

# 겹치는 수정은 한 회차에 하나만 적용하고 다음 회차에 다시 돌린다 (ruff --fix와 같은 방식)
MAX_ROUNDS: Final = 5
BASE_NOT_FORMATTED: Final = "base 브랜치가 ruff format을 따르지 않음"


def pick_targets(violations: Sequence[Violation], decisions: Sequence[FixDecision]) -> Picked:
    """기계적 수정 대상: 수정 판단이 `fix`·`mechanical`로 정한 줄 단위 위반. 크기 상한은 검증의
    7번이 본다."""
    fix = {d.violation_id for d in decisions if d.action == "fix" and d.method == "mechanical"}
    return Picked(tuple(v for v in violations if v.id in fix and v.unit == "line"), ())


def fix_violations(
    found: Found,
    picked: Picked,
    *,
    worktree: Path,
    merge_base: str,
    base_sha: str,
    test_before: PytestRun,
    checks: Sequence[RuleCheck],
    sandbox: FormatsFiles,
    run_tests: Callable[[], PytestRun],
    is_stale: Callable[[], bool],
    secrets: Sequence[str],
    disabled: Collection[str] = (),
    max_rounds: int = MAX_ROUNDS,
) -> MechanicalFix | None:
    """picked.targets를 도구의 수정으로 고치고 검증한다. 새 커밋이 와서 중단되면 None.

    checks는 수정 전 위반을 찾은 규칙의 확인 방법 전부다(`doc_rules.saved_checks`). 재검사가 모두
    다시 돌린다.
    """
    targets = picked.targets
    wanted = {v.id for v in targets}
    allowed = sorted({v.file for v in targets})
    ruff_checks = [c for c in checks if isinstance(c, Checked) and c.checker.kind == "ruff"]
    ignored_before = scope.snapshot_ignored(worktree)

    def outcome(status: MechanicalStatus, detail: str = "", **more) -> MechanicalFix:
        return MechanicalFix(status=status, targets=tuple(sorted(wanted)), detail=detail, **more)

    # 수정 전 PR이 추가한 줄. 정렬 같은 수정이 자리만 옮긴 base의 줄은 PR의 줄로 다시 세지 않는다
    pr_added = added_texts(repo.working_diff(worktree, merge_base))

    def recheck(chosen: Sequence[RuleCheck]) -> detect.Detection:
        diff = repo.working_diff(worktree, merge_base)
        return detect.detect(
            chosen, worktree, added_lines_except_moved(diff, pr_added),
            new_files=new_files(diff), sandbox=sandbox, disabled=disabled,
        )  # fmt: skip

    touched: set[str] = set()
    for _ in range(max_rounds):
        found_now = recheck(ruff_checks)
        groups = {}
        for v in found_now.violations:
            if v.id in wanted and v.id in found_now.edits:
                groups.setdefault(v.file, []).append(found_now.edits[v.id])
        if not groups or not _apply(worktree, groups, touched):
            break
    formatted: list[str] = []
    format_skipped = ""
    format_files = sorted(touched | {v.file for v in targets if v.code == python.FORMAT_CODE})
    if format_files:
        with repo.side_worktree(worktree, base_sha) as base_tree:
            base_ok = sandbox.format_check(base_tree)
        if base_ok is True:
            before = {f: _read(worktree, f) for f in format_files}
            done = sandbox.format_apply(worktree, format_files)
            if done is True:
                formatted = [f for f in format_files if _read(worktree, f) != before[f]]
            else:
                format_skipped = str(done)
        else:
            format_skipped = BASE_NOT_FORMATTED if base_ok is False else base_ok
    if not touched and not formatted:
        return outcome("nothing_fixed", format_skipped=format_skipped)
    changed_files = tuple(sorted(touched | set(formatted)))
    common = {"changed_files": changed_files, "formatted_files": tuple(formatted),
              "format_skipped": format_skipped}  # fmt: skip

    checked = scope.check_scope(worktree, set(allowed), ignored_before, policy=MECHANICAL_POLICY)
    if not checked.ok:
        return outcome("out_of_scope", checked.detail, **common)
    patch = repo.head_diff(worktree)
    kind = find_secret(patch, secrets)
    if kind is not None:
        return outcome("secret", f"{SECRET_BLOCKED} ({kind})", **common)
    after = recheck(checks)
    if after.unchecked:
        what = "; ".join(f"{u.rule.id}: {u.detail}" for u in after.unchecked)
        return outcome("unchecked", what, **common)
    left, new = model_fix_pure.recheck(found.violations, targets, after.violations)
    if new:
        detail = ", ".join(f"{v.file}:{v.line} {v.rule_id}" for v in new[:10])
        return outcome("new_violations", detail, **common)
    if is_stale():
        return None
    run = run_tests()
    if run.cancelled:
        return None
    if not same_test_result(test_before, run):
        return outcome("tests_changed", format_test_line(run), **common)
    entries = [e for e in parse_porcelain(repo.status(worktree)) if not is_pipeline_output(e.path)]
    too_big = model_fix_pure.size_problem(len(entries), changed_line_count(patch))
    if too_big is not None:
        return outcome("too_big", too_big, **common)
    left_ids = {v.id for v in left}
    return outcome(
        "ready",
        fixed=tuple(sorted(wanted - left_ids)),
        remaining=tuple(sorted(left_ids)),
        tests=run.passed_count,
        test_line=format_test_line(run),
        **common,
    )


def _apply(worktree: Path, groups: dict[str, list[tuple[TextEdit, ...]]], touched: set[str]) -> int:
    """파일마다 편집을 적용한다. 적용한 편집 묶음 수."""
    applied = 0
    for file, edits in groups.items():
        path = repo.worktree_file(worktree, file)
        if path is None:  # 쓰기 직전에 링크·밖을 가리키는 경로가 아닌지 다시 본다
            continue
        # newline=""로 읽고 써서 CRLF 같은 줄바꿈을 그대로 둔다
        with path.open(encoding="utf-8", newline="") as f:
            text = f.read()
        new_text, count = apply_edits(text, edits)
        if count and new_text != text:
            with path.open("w", encoding="utf-8", newline="") as f:
                f.write(new_text)
            applied += count
            touched.add(file)
    return applied


def _read(worktree: Path, rel: str) -> bytes:
    path = repo.worktree_file(worktree, rel)
    return path.read_bytes() if path is not None else b""


def save_attempt(
    store: Store,
    job: PRJob,
    merge_base: str,
    fix: MechanicalFix,
    notes: Sequence[records_pure.Target],
    worktree: Path,
) -> MechanicalFix:
    """시도 결과를 `fix_attempts`에 저장하고 그 id를 붙인다. 통과했으면 패치와 바뀐 파일의 해시도
    남긴다. 전달은 이 패치만 올린다 (4.8 끝)."""
    ready = fix.ready
    attempt_id = store.save_fix_attempt(
        FixAttempt(
            repo=job.repo, pr_number=job.pr_number, head_sha=job.head_sha, merge_base=merge_base,
            bundle=MECHANICAL_BUNDLE, status=fix.status, attempts=1, models_json="[]",
            detail=fix.detail, targets_json=records_pure.targets_to_json(notes),
            patch=repo.head_diff(worktree) if ready else None,
            file_hashes_json=(
                json.dumps(file_hashes(worktree, fix.changed_files)) if ready else None
            ),
        )
    )  # fmt: skip
    return replace(fix, attempt_id=attempt_id)
