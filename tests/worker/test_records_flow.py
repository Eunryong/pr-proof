"""전달 재시도(`fix_attempts`), 같은 커밋의 `/recheck`, 과거 기록(`violations`, `records`)이
작업 전체에서 도는지 (docs/DESIGN.md 4.8 끝, 4.9, 4.10, 5.6).

`update.process_update`와 `/recheck` 명령으로 작업 전체를 돌린다. 실제 git 체크아웃(로컬 원격),
실제 git apply, 실제 SQLite. 모델, 검사기 샌드박스, 테스트 실행, GitHub, 푸시만 가짜다. Docker 없음.
"""

from pathlib import Path
from typing import Final

import pytest
from sqlmodel import Session

from app.models.db import FixAttempt
from app.models.job import PRJob
from app.models.records import targets_from_json
from app.worker import command, repo, retry, update
from app.worker.github import GitHubError

from .helpers import bodies
from .test_doc_rules_flow import (  # noqa: F401
    BASE_FILES,
    _commit,
    _git,
    doc_env,
    make_remote,
    print_rule,
    rule_answer,
    use_model,
)
from .test_model_fix_flow import (  # noqa: F401
    FIX_LINE_2,
    RID,
    FakeFixer,
    FakeModel,
    _explanation,
    fix_env,
    use_fixer,
)

ONE_PRINT: Final = {"orders/a.py": "x = 1\nprint(x)\n"}
SECRET: Final = "ghp_" + "A" * 36


def _start(env, tmp_path: Path, pr: dict[str, str] = ONE_PRINT) -> PRJob:
    base_sha, head = make_remote(tmp_path / "remote", BASE_FILES, pr)
    env["gh"].head, env["gh"].base = head, base_sha
    return PRJob(repo="o/r", pr_number=1, kind="update", head_sha=head, base_sha=base_sha,
                 source="webhook")  # fmt: skip


def _next_commit(env, tmp_path: Path, job: PRJob, files: dict[str, str]) -> PRJob:
    """원본 PR에 새 커밋을 올린다."""
    remote = tmp_path / "remote"
    head = _commit(remote, files)
    _git(remote, "update-ref", "refs/pull/1/head", head)
    env["gh"].head = head
    return job.model_copy(update={"head_sha": head})


def _made_calls(env) -> list[int]:
    """저장된 결과를 쓴 줄을 뺀 실제 모델 호출 기록의 id."""
    return [c.id for c in env["deps"].store.model_calls("o/r", 1) if not c.cache_hit]


def _fail_once(env, target, name: str, error: Exception) -> None:
    real = getattr(target, name)
    left = {"n": 1}

    def flaky(*args, **kwargs):
        if left["n"]:
            left["n"] -= 1
            raise error
        return real(*args, **kwargs)

    env["monkeypatch"].setattr(target, name, flaky)


def _as_retry(env) -> None:
    """RQ가 같은 작업을 두 번째로 실행하는 것처럼 한다."""
    env["monkeypatch"].setattr(retry, "attempt", lambda: (2, True))


def test_failed_push_is_retried_with_the_stored_patch(fix_env, tmp_path: Path) -> None:  # noqa: F811
    """2-3: 검증을 통과한 뒤 전달(push)이 실패해 다시 시도하면 모델 호출 기록이 늘지 않고 저장한
    패치가 그대로 올라간다. 끝난 node(규칙 읽기, 수정 판단, 수정 작성, 테스트)는 다시 돌지
    않는다."""
    model = use_model(fix_env, FakeModel(rule_answer(print_rule())))
    fixer = use_fixer(fix_env, FakeFixer([FIX_LINE_2], [_explanation(("orders/a.py", 2))]))
    _fail_once(fix_env, repo, "commit_and_push", repo.GitError("push 실패"))
    deps, job = fix_env["deps"], _start(fix_env, tmp_path)

    with pytest.raises(repo.GitError, match="push 실패"):
        update.process_update(job, deps)
    assert fix_env["pushes"] == [] and fix_env["gh"].pulls == {}
    calls, rows = _made_calls(fix_env), deps.store.model_calls("o/r", 1)
    prompts = len(model.prompts)
    attempt = deps.store.fix_attempt(1)
    assert attempt is not None and attempt.status == "ready" and "+log(x)" in attempt.patch

    _as_retry(fix_env)
    assert update.process_update(job, deps) == "passed"
    assert deps.store.model_calls("o/r", 1) == rows and _made_calls(fix_env) == calls
    assert len(model.prompts) == prompts and len(fixer.calls) == 1
    ((push),) = fix_env["pushes"]
    assert push["diff"] == attempt.patch and push["paths"] == ["orders/a.py"]
    ((number, _),) = fix_env["gh"].pulls.items()
    (proposal,) = deps.store.proposals("o/r", 1)
    assert (proposal.number, proposal.attempt_id) == (number, 1)
    assert f"수정안 PR #{number}을 열었습니다." in bodies(fix_env["gh"])[-1]


def test_repeated_delivery_opens_the_pull_request_once(fix_env, tmp_path: Path) -> None:  # noqa: F811
    """수정안 PR을 연 뒤 기록하다 실패해도, 재시도는 같은 브랜치에 같은 내용을 다시 올리고 열린 PR을
    고칠 뿐 새로 만들지 않는다 (5.6)."""
    use_model(fix_env, FakeModel(rule_answer(print_rule())))
    use_fixer(fix_env, FakeFixer([FIX_LINE_2], [_explanation(("orders/a.py", 2))]))
    deps, job = fix_env["deps"], _start(fix_env, tmp_path)
    _fail_once(fix_env, deps.store, "record_proposal", GitHubError("gh 실패"))

    with pytest.raises(GitHubError):
        update.process_update(job, deps)
    _as_retry(fix_env)
    update.process_update(job, deps)
    first, second = fix_env["pushes"]
    assert first["diff"] == second["diff"] and first["branch"] == second["branch"]
    assert list(fix_env["gh"].pulls) == [900] and len(deps.store.proposals("o/r", 1)) == 1


def test_redelivery_refuses_a_patch_whose_files_no_longer_match(fix_env, tmp_path: Path) -> None:  # noqa: F811
    """다시 적용한 결과의 파일 해시가 저장한 값과 다르면 올리지 않는다 (4.8 끝)."""
    use_model(fix_env, FakeModel(rule_answer(print_rule())))
    use_fixer(fix_env, FakeFixer([FIX_LINE_2], [_explanation(("orders/a.py", 2))]))
    _fail_once(fix_env, repo, "commit_and_push", repo.GitError("push 실패"))
    deps, job = fix_env["deps"], _start(fix_env, tmp_path)
    with pytest.raises(repo.GitError):
        update.process_update(job, deps)
    with Session(deps.store.engine) as session:
        row = session.get(FixAttempt, 1)
        row.file_hashes_json = '{"orders/a.py": "다른 해시"}'
        session.add(row)
        session.commit()

    _as_retry(fix_env)
    update.process_update(job, deps)
    assert fix_env["pushes"] == [] and fix_env["gh"].pulls == {}
    assert (
        "수정안 PR을 만들지 않았습니다 (다시 적용한 수정안이 검증한 파일과 다름)"
        in bodies(fix_env["gh"])[-1]
    )


def test_recheck_on_the_same_commit_makes_no_model_calls(fix_env, tmp_path: Path) -> None:  # noqa: F811
    """2-4, 2-10: 같은 커밋에 `/recheck`를 달면 규칙 읽기, 검사기 작성, 수정 판단, 수정 작성의 모델
    호출이 0이다. 저장된 결과로 같은 결과가 나오고, 열린 수정안 PR을 다시 만들지 않는다."""
    model = use_model(fix_env, FakeModel(rule_answer(print_rule())))
    fixer = use_fixer(fix_env, FakeFixer([FIX_LINE_2], [_explanation(("orders/a.py", 2))]))
    deps, job = fix_env["deps"], _start(fix_env, tmp_path)
    assert update.process_update(job, deps) == "passed"
    calls, prompts = _made_calls(fix_env), len(model.prompts)
    assert {kind for kind, _ in model.prompts} == {"rules", "tool", "checker", "decide"}

    recheck = PRJob(repo="o/r", pr_number=1, kind="command", command="/recheck", comment_id=77,
                    source="webhook")  # fmt: skip
    assert command.process_command(recheck, deps) == "recheck:passed"
    assert _made_calls(fix_env) == calls and len(model.prompts) == prompts
    assert len(fixer.calls) == 1 and len(fix_env["pushes"]) == 1
    comment = bodies(fix_env["gh"])[-1]
    assert "모델 호출 0회 / 상한 14회 (규칙 읽기와 검사기 작성) · 저장된 결과 3건 사용" in comment
    decided = "수정 판단: 고칠 대상 1건, 직접 고쳐야 함 0건 · 모델 호출 0회 · 저장된 판단 1건 사용"
    assert decided in comment
    assert "이 커밋의 모델 수정안 PR #900이 열려 있습니다." in comment


def test_closed_fix_pr_is_rejected_and_next_commit_skips_it(fix_env, tmp_path: Path) -> None:  # noqa: F811
    """2-6: 사람이 수정안 PR을 닫으면 거절 기록이 하나 남고, 다음 커밋에서 같은 줄은
    `skip`(이전에 거절됨)이다. 수정 작성을 다시 부르지 않는다."""
    use_model(fix_env, FakeModel(rule_answer(print_rule())))
    fixer = use_fixer(fix_env, FakeFixer([FIX_LINE_2], [_explanation(("orders/a.py", 2))]))
    deps, job = fix_env["deps"], _start(fix_env, tmp_path)
    update.process_update(job, deps)
    fix_env["gh"].pulls[900]["state"] = "closed"

    nxt = _next_commit(fix_env, tmp_path, job, {"orders/a.py": "x = 1\nprint(x)\ny = 2\n"})
    state_before = len(fixer.calls)
    update.process_update(nxt, deps)
    (record,) = deps.store.records("o/r", 1)
    assert (record.kind, record.origin, record.fix_pr_number) == ("rejected", "fix_pr", 900)
    assert (record.rule_id, record.file, record.line_text) == (RID, "orders/a.py", "print(x)")
    assert record.commit_sha == job.head_sha and "-print(x)" in record.diff
    assert "print(x)" in record.context and record.embedded_at is None
    assert len(fixer.calls) == state_before and list(fix_env["gh"].pulls) == [900]
    assert "  - `orders/a.py:2` · 이전에 거절됨 (#900)" in bodies(fix_env["gh"])[-1]

    update.process_update(nxt, deps)  # 같은 일을 다시 확인해도 기록은 하나다
    assert len(deps.store.records("o/r", 1)) == 1


def test_merged_fix_is_adopted_and_not_credited_to_the_author(fix_env, tmp_path: Path) -> None:  # noqa: F811
    """수정안 PR이 머지되면 채택 기록이 남는다. 그 머지 커밋에서 사라진 위반은 작성자가 고친 것이
    아니다 (4.10)."""
    use_model(fix_env, FakeModel(rule_answer(print_rule())))
    use_fixer(fix_env, FakeFixer([FIX_LINE_2], [_explanation(("orders/a.py", 2))]))
    deps, job = fix_env["deps"], _start(fix_env, tmp_path)
    update.process_update(job, deps)
    merged = _next_commit(fix_env, tmp_path, job, {"orders/a.py": "x = 1\nlog(x)\n"})
    fix_env["gh"].pulls[900] |= {"state": "closed", "merged": True,
                                 "merge_commit": merged.head_sha}  # fmt: skip

    update.process_update(merged, deps)
    (record,) = deps.store.records("o/r", 1)
    assert (record.kind, record.origin, record.commit_sha) == ("adopted", "fix_pr", merged.head_sha)


def test_author_fixing_the_line_in_a_new_commit_is_adopted(fix_env, tmp_path: Path) -> None:  # noqa: F811
    """이전 커밋의 위반이 새 커밋에서 사라지고 그 줄이 바뀌면 작성자의 채택 기록이다 (4.10)."""
    skip_all = FakeModel(rule_answer(print_rule()),
                         decide=lambda ids: [{"id": i, "action": "skip", "why": "사람이"}
                                             for i in ids])  # fmt: skip
    use_model(fix_env, skip_all)
    deps, job = fix_env["deps"], _start(fix_env, tmp_path)
    update.process_update(job, deps)
    assert deps.store.records("o/r", 1) == []

    nxt = _next_commit(fix_env, tmp_path, job, {"orders/a.py": "x = 1\nlog(x)\n"})
    update.process_update(nxt, deps)
    (record,) = deps.store.records("o/r", 1)
    assert (record.kind, record.origin, record.commit_sha) == ("adopted", "author_commit",
                                                              nxt.head_sha)  # fmt: skip
    assert record.line_text == "print(x)" and "print(x)" in record.context
    assert "-print(x)" in record.diff and "+log(x)" in record.diff

    # 위반이 없는 커밋 뒤의 커밋도 같은 위반을 다시 고친 것으로 세지 않는다
    later = _next_commit(fix_env, tmp_path, nxt, {"orders/a.py": "x = 1\nlog(x)\nz = 3\n"})
    update.process_update(later, deps)
    assert len(deps.store.records("o/r", 1)) == 1


def test_fix_that_fails_verification_is_a_failure_record(fix_env, tmp_path: Path) -> None:  # noqa: F811
    use_model(fix_env, FakeModel(rule_answer(print_rule())))
    bad = _explanation(("orders/a.py", 2), ("orders/a.py", 1))
    use_fixer(fix_env, FakeFixer([FIX_LINE_2], [bad]))
    deps, job = fix_env["deps"], _start(fix_env, tmp_path)
    update.process_update(job, deps)

    attempt = deps.store.fix_attempt(1)
    assert attempt.status == "failed" and attempt.patch is None and attempt.attempts == 3
    (target,) = targets_from_json(attempt.targets_json)
    (record,) = deps.store.records("o/r", 1)
    assert (record.kind, record.origin, record.source_id) == (
        "failed", "verification", f"attempt:1:{target.violation_id}",
    )  # fmt: skip
    assert "설명이 바뀌지 않은 줄을 가리킴" in record.detail and "+log(x)" in record.diff


def test_record_with_a_secret_is_not_stored(fix_env, tmp_path: Path) -> None:  # noqa: F811
    """저장 전에 비밀값 검사를 한다 (6.5). 걸린 기록은 저장하지 않는다."""
    use_model(fix_env, FakeModel(rule_answer(print_rule())))
    leak = {"orders/a.py": [("print(x)\n", f'log("{SECRET}")\n')]}
    use_fixer(fix_env, FakeFixer([leak], [_explanation(("orders/a.py", 2))]))
    deps, job = fix_env["deps"], _start(fix_env, tmp_path)
    update.process_update(job, deps)
    assert deps.store.fix_attempt(1).status == "dropped"
    assert deps.store.records("o/r", 1) == []
