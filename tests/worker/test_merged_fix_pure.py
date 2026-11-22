"""수정안을 머지한 커밋에 검증한 결과를 옮겨 적을지의 판정 (docs/DESIGN.md 4.9 "수정안 PR 상태
관리"). 조건이 하나라도 맞지 않으면 까닭이 나오고 부르는 쪽은 처음부터 검사한다."""

from dataclasses import replace
from datetime import UTC, datetime
from typing import Final

import pytest

from app.shared.db_pure import FixAttempt, Proposal, Run
from app.worker.checker_pure import CheckerKind
from app.worker.doc_rules_pure import CallUsage, CheckerRef, Found, RuleChecked, RuleJudged
from app.worker.fix.fix_decision_pure import Decided, FixDecision
from app.worker.fix.fix_pure import MechanicalFix
from app.worker.fix.model_fix_pure import ModelFix, Verification
from app.worker.merged_fix_pure import (
    NOT_MERGE_COMMIT,
    VerifiedCheck,
    attempt_problem,
    carried,
    fixed_ids,
    merge_problem,
    merged_proposal,
    runs_problem,
    state_problem,
)
from app.worker.rule_pure import CheckMethod, Rule, RuleSource
from app.worker.test_run_pure import PytestRun, ReportSummary
from app.worker.violation_pure import Violation

NOW: Final = datetime(2026, 11, 22, tzinfo=UTC)
V: Final = "v" * 40  # 수정안을 검증한 커밋
M: Final = "m" * 40  # 수정안의 머지 커밋
CODE: Final = "c" * 40  # 지금 파이프라인 코드의 커밋
PASSED: Final = PytestRun(exit_code=0, summary=ReportSummary(passed=9, total=9))
RULE: Final = Rule(id="R1", text="print를 쓰지 않는다", source=RuleSource("README.md", "s", "x"),
                   origin="document", scope=("orders/**",), check=CheckMethod("checker"),
                   fix="model")  # fmt: skip
KIND: Final[CheckerKind] = "ast-grep"
FIXED: Final = Violation("fixed", "R1", "orders/a.py", 2, "m", "checker", line_text="print(x)")
# 고치지 않기로 한 위반. 수정안이 위쪽에 줄을 하나 더해 머지 커밋에서는 한 줄 아래다
LEFT: Final = Violation("left", "R1", "orders/a.py", 5, "m", "checker", line_text="print(y)")


def proposal(**fields: object) -> Proposal:
    values: dict = dict(id=1, repo="o/r", pr_number=7, skill="model", head_sha=V, branch="b",
                        number=8, state="merged", merge_commit=M, created_at=NOW,
                        updated_at=NOW, attempt_id=3)  # fmt: skip
    return Proposal(**(values | fields))


def attempt(**fields: object) -> FixAttempt:
    values: dict = dict(id=3, repo="o/r", pr_number=7, head_sha=V, merge_base="b", bundle="model",
                        status="ready", attempts=1, models_json="[]", targets_json="[]",
                        patch="diff", file_hashes_json="{}", created_at=NOW)  # fmt: skip
    return FixAttempt(**(values | fields))


def run(state: str = "done", commit: str | None = CODE, head: str = V) -> Run:
    return Run(repo="o/r", pr_number=7, head_sha=head, kind="update", source="webhook",
               state=state, started_at=NOW, heartbeat_at=NOW, pipeline_commit=commit)  # fmt: skip


def found(*violations: Violation, judged: RuleJudged | None = None) -> Found:
    checked = RuleChecked(RULE, CheckerRef("k", "id", KIND, cached=True))
    outcomes = (checked,) if judged is None else (checked, judged)
    return Found(outcomes=outcomes, violations=violations, disabled=0,
                 usage=CallUsage(0, 10, 0, (), 0, 0))  # fmt: skip


def decided(*decisions: FixDecision) -> Decided:
    return Decided(decisions)


FIX_IT: Final = FixDecision("fixed", "fix", "model", method="model")
SKIP_IT: Final = FixDecision("left", "skip", "model", reason="changes_behavior", why="동작이 바뀜")
MODEL_FIX: Final = ModelFix(status="ready", targets=("fixed",), attempt_id=3,
                            verification=Verification(tests=9, resolved=1, rules_rechecked=1,
                                                      files=1, lines=2))  # fmt: skip


def check(**fields: object) -> VerifiedCheck:
    values: dict = dict(finished=True, base_sha="base", found=found(FIXED, LEFT),
                        test_run=PASSED, decisions=decided(FIX_IT, SKIP_IT), fix=MODEL_FIX,
                        pr_checks=None)  # fmt: skip
    return VerifiedCheck(**(values | fields))


# --- 수정안 ---


def test_the_merged_rule_fix_of_this_commit_is_picked() -> None:
    p = proposal()
    assert merged_proposal([proposal(id=9, state="superseded", head_sha="o" * 40), p], M) == p


@pytest.mark.parametrize(
    ("proposals", "why"),
    [
        ([], NOT_MERGE_COMMIT),
        ([proposal(state="open", merge_commit=None)], NOT_MERGE_COMMIT),
        ([proposal(merge_commit="x" * 40)], NOT_MERGE_COMMIT),  # 그 뒤에 커밋이 더 올라옴
        ([proposal(), proposal(id=2, skill="mechanical")], "머지 커밋이 같은 수정안이 둘 이상"),
        ([proposal(skill="feedback")], "규칙 수정안이 아님 (feedback)"),
        ([proposal(skill="lint", attempt_id=None)], "규칙 수정안이 아님 (lint)"),
        ([proposal(attempt_id=None)], "수정안에 저장한 수정 시도가 없음"),
        # 같은 커밋의 다른 묶음 수정안: 하나만 머지됐거나 그것도 차례로 머지됨
        ([proposal(), proposal(id=2, skill="mechanical", state="superseded", merge_commit=None)],
         "검증한 커밋에 다른 묶음의 수정안이 있음"),
        ([proposal(), proposal(id=2, skill="mechanical", merge_commit="n" * 40)],
         "검증한 커밋에 다른 묶음의 수정안이 있음"),
    ],
)  # fmt: skip
def test_no_merged_rule_fix_means_a_full_check(proposals: list[Proposal], why: str) -> None:
    assert merged_proposal(proposals, M) == why


def test_a_feedback_fix_of_the_same_commit_does_not_block() -> None:
    p = proposal()
    feedback = proposal(id=2, skill="feedback", state="superseded", merge_commit=None)
    assert merged_proposal([p, feedback], M) == p


def test_attempt_must_hold_the_verified_patch_of_that_proposal() -> None:
    p = proposal()
    assert attempt_problem(attempt(), p) is None
    assert attempt_problem(None, p) == "검증을 통과한 패치를 저장한 기록이 없음"
    assert attempt_problem(attempt(patch=None), p) == "검증을 통과한 패치를 저장한 기록이 없음"
    assert attempt_problem(attempt(file_hashes_json=None), p) is not None
    assert attempt_problem(attempt(head_sha="o" * 40), p) == "저장한 수정 시도가 수정안과 맞지 않음"
    assert attempt_problem(attempt(bundle="mechanical"), p) is not None


# --- 검사 기록 ---


def test_runs_of_the_current_code_allow_carrying() -> None:
    assert runs_problem([run(), run(state="done")], [run("running", head=M)], CODE) is None


@pytest.mark.parametrize(
    ("verified", "head", "why"),
    [
        # 동기화가 배포 뒤 다시 넣은 작업(new_pipeline): 이 커밋에 끝난 검사가 이미 있다
        ([run()], [run("done", commit="old", head=M), run("running", head=M)],
         "이 커밋의 끝난 검사가 이미 있음"),
        ([run()], [run("error", commit="old", head=M)], "이 커밋의 끝난 검사가 이미 있음"),
        ([], [], "검증한 커밋의 끝난 검사가 없음"),
        ([run("error")], [], "검증한 커밋의 끝난 검사가 없음"),
        ([run(commit="old")], [], "검증한 커밋의 검사가 지금 파이프라인 코드의 것이 아님"),
        ([run(commit="old"), run()], [], "검증한 커밋의 검사가 지금 파이프라인 코드의 것이 아님"),
        ([run(commit=None)], [], "검증한 커밋의 검사가 지금 파이프라인 코드의 것이 아님"),
    ],
)  # fmt: skip
def test_runs_that_do_not_allow_carrying(verified: list[Run], head: list[Run], why: str) -> None:
    assert runs_problem(verified, head, CODE) == why


def test_unknown_pipeline_commit_matches_only_unknown_records() -> None:
    assert runs_problem([run(commit=None)], [], "") is None
    assert runs_problem([run()], [], "") is not None


# --- 검증한 커밋의 체크포인트 ---

OTHER_FIX: Final = "검증한 커밋의 수정 결과가 수정안의 시도와 다름"


def test_finished_check_with_one_bundle_allows_carrying() -> None:
    assert state_problem(check(), bundle="model", attempt_id=3, base_sha="base") is None


@pytest.mark.parametrize(
    ("fields", "why"),
    [
        ({"finished": False}, "검증한 커밋의 검사가 끝까지 가지 않음"),
        ({"found": None}, "검증한 커밋의 규칙 결과, 테스트 결과, 수정 판단이 없음"),
        ({"test_run": None}, "검증한 커밋의 규칙 결과, 테스트 결과, 수정 판단이 없음"),
        ({"base_sha": "older"}, "base가 그 뒤 바뀜"),
        ({"fix": None}, OTHER_FIX),
        ({"fix": replace(MODEL_FIX, attempt_id=4)}, OTHER_FIX),
        ({"fix": replace(MODEL_FIX, status="failed")}, OTHER_FIX),
        ({"fix": replace(MODEL_FIX, verification=Verification(9, 1, 1, 1, 2, unverified=1))},
         "도구로 재검사하지 못한 위반을 고친 수정안"),
        ({"pr_checks": object()}, "PR 단위 규칙이 있음"),
        ({"found": found(FIXED, LEFT, judged=RuleJudged(RULE, "r", left=1, left_kind="cap"))},
         "판단하지 못한 hunk가 남음"),
        ({"decisions": decided(FIX_IT, replace(SKIP_IT, reason="call_limit"))},
         "호출 상한으로 정하지 못한 수정 판단이 남음"),
        ({"decisions": decided(FIX_IT, replace(SKIP_IT, action="fix", method="mechanical"))},
         "다른 묶음으로 고칠 위반이 있음"),
    ],
)  # fmt: skip
def test_checks_that_do_not_allow_carrying(fields: dict, why: str) -> None:
    assert state_problem(check(**fields), bundle="model", attempt_id=3, base_sha="base") == why


def test_mechanical_fix_counts_only_what_disappeared() -> None:
    fix = MechanicalFix(status="ready", targets=("a", "b"), fixed=("a",), remaining=("b",))
    assert fixed_ids(fix) == {"a"}
    assert fixed_ids(MODEL_FIX) == {"fixed"}


# --- 머지 커밋의 내용 ---


def test_merge_must_have_the_verified_parent_and_the_patched_tree() -> None:
    assert merge_problem(V, ("o" * 40, V), "t", "t") is None  # GitHub의 머지 커밋
    assert merge_problem(V, (V,), "t", "t") is None  # 스쿼시, 리베이스
    assert (
        merge_problem(V, ("w" * 40, "f" * 40), "t", "t") == "머지 커밋의 부모에 검증한 커밋이 없음"
    )
    assert merge_problem(V, (V, "f" * 40), "t2", "t") == "머지 커밋의 내용이 검증한 수정안과 다름"


# --- 남은 위반 ---

PATCH: Final = """\
diff --git a/orders/a.py b/orders/a.py
--- a/orders/a.py
+++ b/orders/a.py
@@ -1,2 +1,3 @@
 x = 1
-print(x)
+import logging
+logging.info(x)
"""
MERGED_A: Final = ["x = 1", "import logging", "logging.info(x)", "z = 3", "", "print(y)", ""]


def _lines(files: dict[str, list[str]]):
    return lambda path: files.get(path)


def test_fixed_violations_go_and_the_rest_move_to_the_merge_commit_lines() -> None:
    before = found(FIXED, LEFT)
    out = carried(before, decided(FIX_IT, SKIP_IT), {"fixed"}, PATCH,
                  _lines({"orders/a.py": MERGED_A}))  # fmt: skip
    assert not isinstance(out, str)
    moved, decisions = out
    assert moved.violations == (replace(LEFT, line=6),)
    assert decisions.decisions == (SKIP_IT,)
    assert moved.outcomes == before.outcomes


def test_left_violation_whose_line_the_patch_changed_means_a_full_check() -> None:
    touched = replace(LEFT, line=2, line_text="print(x)")
    out = carried(found(touched), decided(), set(), PATCH, _lines({"orders/a.py": MERGED_A}))
    assert out == "수정안이 남은 위반의 줄을 바꿈 (orders/a.py:2)"


@pytest.mark.parametrize(
    "files",
    [
        {},  # 파일이 없음
        {"orders/a.py": MERGED_A[:4]},  # 줄이 모자람
        {"orders/a.py": [*MERGED_A[:5], "print(z)", ""]},  # 내용이 다름
    ],
)
def test_left_violation_not_found_in_the_merge_commit_means_a_full_check(files: dict) -> None:
    out = carried(found(LEFT), decided(SKIP_IT), set(), PATCH, _lines(files))
    assert out == "남은 위반의 줄을 머지 커밋에서 찾지 못함 (orders/a.py:5)"


def test_left_violation_without_line_text_is_not_trusted() -> None:
    out = carried(found(replace(LEFT, line_text=None)), decided(), set(), PATCH,
                  _lines({"orders/a.py": MERGED_A}))  # fmt: skip
    assert isinstance(out, str)


def test_file_violation_stays_while_the_file_exists() -> None:
    path = Violation("path", "R1", "orders/Bad.py", 0, "m", "checker", unit="file")
    out = carried(found(path), decided(), set(), PATCH, _lines({"orders/Bad.py": ["x"]}))
    assert not isinstance(out, str) and out[0].violations == (path,)
    assert carried(found(path), decided(), set(), PATCH, _lines({})) == (
        "남은 파일 위반의 파일을 머지 커밋에서 읽지 못함 (orders/Bad.py)"
    )
