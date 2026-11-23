"""수정안을 머지한 커밋이 검사 없이 검증한 결과를 옮겨 적는지 (docs/DESIGN.md 4.9 "수정안 PR 상태
관리", 5.6).

실제 git 저장소(로컬 원격)에 원래 PR의 커밋, 수정안 브랜치, 머지 커밋을 만들고, 수정안을 검증한
커밋의 기록(runs, fix_attempts, proposals)과 체크포인트를 실제 SQLite에 넣는다. 모델과 테스트
실행은 부르면 실패한다. GitHub만 가짜다. Docker 없음.
"""

from contextlib import ExitStack
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import pytest
from sqlmodel import Session

from app.api.sync_rules_pure import RunSnapshot, enqueue_reason
from app.shared import state_keys_pure, version
from app.shared.db_pure import FixAttempt
from app.shared.job_pure import PRJob
from app.worker import graph, update
from app.worker.core.violation_pure import violation_id
from app.worker.fix.model_fix_pure import Verification
from app.worker.fix.proposal_pure import ProposalResult
from app.worker.merged_fix_pure import CarriedFrom
from app.worker.model.claude import claude_cli
from app.worker.rules.pr_check_pure import PrChecks, PrRuleResult

from .helpers import PASSED, bodies, use_runner
from .rules.test_doc_rules_flow import BASE_FILES, _commit, _git, doc_env, make_remote  # noqa: F401
from .test_merged_fix_pure import FIX_IT, MODEL_FIX, RULE, SKIP_IT, decided, found
from .test_merged_fix_pure import FIXED as FIXED_SAMPLE
from .test_merged_fix_pure import LEFT as LEFT_SAMPLE

V_FILE: Final = "x = 1\nprint(x)\nz = 3\n\nprint(y)\n"
FIXED_FILE: Final = "x = 1\nimport logging\nlogging.info(x)\nz = 3\n\nprint(y)\n"
FIXED: Final = replace(FIXED_SAMPLE, id=violation_id("R1", "orders/a.py", "print(x)", 0))
LEFT: Final = replace(LEFT_SAMPLE, id=violation_id("R1", "orders/a.py", "print(y)", 0))
FIX_NUMBER: Final = 8


def _fail(*args, **kwargs):
    pytest.fail("수정안의 머지 커밋에서는 모델도 테스트도 부르지 않아야 한다")


@pytest.fixture
def merge_env(doc_env):  # noqa: F811
    use_runner(doc_env, _fail)
    doc_env["monkeypatch"].setattr(claude_cli, "run_structured", _fail)
    return doc_env


def _verified(env, tmp_path: Path, **extra: object) -> tuple[PRJob, str]:
    """원래 PR의 커밋 V를 검사해 모델 수정안을 검증하고 수정안 PR을 연 상태. (V의 작업, 패치)."""
    remote = tmp_path / "remote"
    base_sha, v = make_remote(remote, BASE_FILES, {"orders/a.py": V_FILE})
    _git(remote, "checkout", "-q", "-b", "fix")
    f = _commit(remote, {"orders/a.py": FIXED_FILE})
    patch = _git(remote, "diff", v, f) + "\n"
    _git(remote, "checkout", "-q", "main")
    store = env["deps"].store
    job = PRJob(repo="o/r", pr_number=1, kind="update", head_sha=v, base_sha=base_sha,
                source="webhook")  # fmt: skip
    run_id = store.start_run(job)
    store.finish_run(run_id, "done", test_status="passed")
    attempt_id = store.save_fix_attempt(FixAttempt(
        repo="o/r", pr_number=1, head_sha=v, merge_base=base_sha, bundle="model", status="ready",
        attempts=1, models_json="[]", targets_json="[]", patch=patch, file_hashes_json="{}",
    ))  # fmt: skip
    store.record_proposal("o/r", 1, "model", v, "pipeline/pr-1-fix-model", FIX_NUMBER,
                          attempt_id=attempt_id)  # fmt: skip
    env["redis"].set(state_keys_pure.latest_head("o/r", 1), v)
    values = {
        "job": job, "doc_rules": found(FIXED, LEFT), "test_run": PASSED,
        "fix_decisions": decided(replace(FIX_IT, violation_id=FIXED.id),
                                 replace(SKIP_IT, violation_id=LEFT.id)),
        "model_fix": replace(MODEL_FIX, targets=(FIXED.id,), attempt_id=attempt_id),
        "proposals": {"model": ProposalResult(number=FIX_NUMBER, url="u")},
        "commented": True,
    } | extra  # fmt: skip
    ctx = update.flow_context(env["deps"], run_id, None)
    with ExitStack() as stack:
        flow = graph.build_graph(ctx, graph.open_checkpointer(ctx.data_dir, stack))
        flow.update_state(graph.thread_config(job), values, as_node=graph.FIX_COMMENT)
    return job, patch


def _merged(env, tmp_path: Path, job: PRJob, how: str) -> PRJob:
    """수정안 브랜치를 원래 PR의 브랜치에 머지한 커밋을 PR의 head로 하고, 수정안을 머지됨으로
    적는다(`proposal.refresh`가 GitHub에서 읽어 적는 값)."""
    remote = tmp_path / "remote"
    if how == "after_other_commit":  # 검증한 커밋 뒤에 원래 브랜치에 커밋이 더 올라온 뒤 머지
        _commit(remote, {"scripts/b.py": "y = 2\n"})
    if how == "squash":
        _git(remote, "merge", "-q", "--squash", "fix")
        _git(remote, "commit", "-q", "-m", "squash")
    elif how == "mixed":  # 머지에 다른 변경이 섞임
        _git(remote, "merge", "-q", "--no-ff", "--no-commit", "fix")
        (remote / "scripts/b.py").write_text("y = 3\n")
        _git(remote, "add", "-A")
        _git(remote, "commit", "-q", "-m", "merge")
    else:
        _git(remote, "merge", "-q", "--no-ff", "-m", f"Merge pull request #{FIX_NUMBER}", "fix")
    head = _git(remote, "rev-parse", "HEAD")
    _git(remote, "update-ref", "refs/pull/1/head", head)
    store = env["deps"].store
    (p,) = store.proposals("o/r", 1)
    store.set_proposal_state(p.id, "merged", merge_commit=head)
    env["gh"].head, env["gh"].base = head, job.base_sha
    env["redis"].set(state_keys_pure.latest_head("o/r", 1), head)
    return job.model_copy(update={"head_sha": head})


def test_merge_commit_of_the_verified_fix_is_carried_without_checking(
    merge_env, tmp_path: Path
) -> None:
    deps = merge_env["deps"]
    job, _ = _verified(merge_env, tmp_path)
    merged = _merged(merge_env, tmp_path, job, "merge")

    assert update.process_update(merged, deps) == "passed"
    body = bodies(merge_env["gh"])[-1]
    assert (
        f"이 커밋은 수정안 #{FIX_NUMBER}(모델 수정)을 머지한 커밋이고, 내용이 검증한 수정안과 같아 "
        "다시 검사하지 않았습니다. 결과는 커밋 "
        f"`{job.head_sha[:7]}`의 검사와 수정안 #{FIX_NUMBER}의 검증에서 옮겨 적었습니다."
    ) in body
    assert "**테스트:** 통과 (9개)" in body
    # 고친 위반은 사라지고, 고치지 않기로 한 위반은 머지 커밋의 줄 번호로 남는다
    assert "**규칙:** ⚠️ 위반 1건 (규칙 1개)" in body and "`orders/a.py:6`" in body
    assert "`orders/a.py:2`" not in body

    # 기록: 이 커밋의 done run(지금 코드의 커밋), 위반 기록, 체크포인트
    (run,) = deps.store.runs("o/r", 1, merged.head_sha)
    assert (run.state, run.test_status) == ("done", "passed")
    assert run.pipeline_commit == (version.current() or None)
    snapshot = RunSnapshot(run.state, run.heartbeat_at, run.finished_at, run.pipeline_commit)
    now = datetime.now(UTC)
    assert enqueue_reason([snapshot], now, env_error_backoff=timedelta(minutes=1),
                          heartbeat_timeout=timedelta(minutes=1),
                          pipeline_commit=version.current()) is None  # fmt: skip
    seen = deps.store.violations_at("o/r", 1, merged.head_sha)
    assert [(v.violation_id, v.line, v.line_text) for v in seen] == [(LEFT.id, 6, "print(y)")]
    assert "print(y)" in seen[0].context
    ctx = update.flow_context(deps, 0, None)
    values, finished = graph.finished_values(merged, ctx)
    assert finished and values["carried"] == CarriedFrom(FIX_NUMBER, "model", job.head_sha)
    assert [v.line for v in values["doc_rules"].violations] == [6]


def test_pr_level_result_and_model_found_fix_are_carried(merge_env, tmp_path: Path) -> None:
    """PR 단위 규칙이 있고 수정안이 모델 판단으로 찾은 위반을 고친 경우에도 옮겨 적는다. PR 단위
    확인의 결과는 검증한 커밋의 것을 그대로 두고, 재검사하지 못한 수를 코멘트에 적는다."""
    pr_rule = replace(RULE, id="R-pr")
    pr_checks = PrChecks(results=(PrRuleResult(pr_rule, "unchecked", kind="plan_none"),),
                         violations=())  # fmt: skip
    fix = replace(MODEL_FIX, targets=(FIXED.id,),
                  verification=Verification(9, 1, 0, 1, 2, unverified=1))  # fmt: skip
    job, _ = _verified(merge_env, tmp_path, pr_checks=pr_checks)
    ctx = update.flow_context(merge_env["deps"], 0, None)
    (p,) = merge_env["deps"].store.proposals("o/r", 1)
    with ExitStack() as stack:
        flow = graph.build_graph(ctx, graph.open_checkpointer(ctx.data_dir, stack))
        flow.update_state(graph.thread_config(job),
                          {"model_fix": replace(fix, attempt_id=p.attempt_id)},
                          as_node=graph.FIX_COMMENT)  # fmt: skip
    merged = _merged(merge_env, tmp_path, job, "merge")

    assert update.process_update(merged, merge_env["deps"]) == "passed"
    body = bodies(merge_env["gh"])[-1]
    assert "다시 검사하지 않았습니다" in body
    assert (
        "수정안이 고친 위반 중 1건은 모델 판단으로 찾은 것이라 도구로 재검사하지 못했고, 그 줄이 "
        "바뀐 것까지만 확인했습니다."
    ) in body
    assert "모델이 PR의 커밋, 글, 바뀐 파일로는 판단하지 못함" in body
    values, finished = graph.finished_values(merged, ctx)
    # 체크포인트를 거치면 튜플이 목록으로 돌아오므로 값 전체가 아니라 규칙과 상태로 견준다
    kept = [(r.rule.id, r.status, r.kind) for r in values["pr_checks"].results]
    assert finished and kept == [("R-pr", "unchecked", "plan_none")]
    assert values["carried"] == CarriedFrom(FIX_NUMBER, "model", job.head_sha, unverified=1)


def test_squash_merge_is_carried_too(merge_env, tmp_path: Path) -> None:
    job, _ = _verified(merge_env, tmp_path)
    merged = _merged(merge_env, tmp_path, job, "squash")
    plan = update.carry_plan(merged, update.flow_context(merge_env["deps"], 0, None))
    assert isinstance(plan, update.Carry)


@pytest.mark.parametrize(
    ("how", "why"),
    [
        ("after_other_commit", "머지 커밋의 부모에 검증한 커밋이 없음"),
        ("mixed", "머지 커밋의 내용이 검증한 수정안과 다름"),
    ],
)
def test_merge_with_other_changes_is_checked_from_the_start(
    merge_env, tmp_path: Path, how: str, why: str
) -> None:
    job, _ = _verified(merge_env, tmp_path)
    merged = _merged(merge_env, tmp_path, job, how)
    assert update.carry_plan(merged, update.flow_context(merge_env["deps"], 0, None)) == why


def test_patch_that_no_longer_applies_is_checked_from_the_start(merge_env, tmp_path: Path) -> None:
    job, patch = _verified(merge_env, tmp_path)
    merged = _merged(merge_env, tmp_path, job, "merge")
    deps = merge_env["deps"]
    broken = patch.replace("-print(x)", "-print(nothing)")
    with Session(deps.store.engine) as session:
        row = session.get(FixAttempt, 1)
        row.patch = broken
        session.add(row)
        session.commit()
    plan = update.carry_plan(merged, update.flow_context(deps, 0, None))
    assert plan == "저장한 패치를 검증한 커밋에 적용하지 못함"


def test_missing_checkpoint_is_checked_from_the_start(merge_env, tmp_path: Path) -> None:
    job, _ = _verified(merge_env, tmp_path)
    merged = _merged(merge_env, tmp_path, job, "merge")
    ctx = update.flow_context(merge_env["deps"], 0, None)
    with ExitStack() as stack:
        graph.open_checkpointer(ctx.data_dir, stack).delete_thread(
            graph.thread_id("o/r", 1, job.head_sha)
        )
    assert update.carry_plan(merged, ctx) == "검증한 커밋의 검사가 끝까지 가지 않음"


def test_recheck_never_carries(merge_env, tmp_path: Path) -> None:
    """`/recheck`(carry=False)는 수정안의 머지 커밋이어도 처음부터 검사한다."""
    job, _ = _verified(merge_env, tmp_path)
    merged = _merged(merge_env, tmp_path, job, "merge")
    mp, ran = merge_env["monkeypatch"], []
    mp.setattr(update, "carry_plan", _fail)
    mp.setattr(graph, "run_flow", lambda j, ctx, retry=False: ran.append(j) or {"test_run": PASSED})
    refs = merge_env["deps"].github.pr_refs("o/r", 1)
    assert update.run_admitted(merged, merge_env["deps"], refs, carry=False) == "passed"
    assert ran == [merged]
