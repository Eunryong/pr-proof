"""도구 설정의 규칙 묶음과 기계적 수정 묶음이 그래프에서 도는지 (docs/DESIGN.md 4.3, 4.6, 4.7, 4.8,
4.9, 5.1, 13절 3단계).

실제 git 체크아웃(로컬 원격), 실제 base 설정 읽기, 실제 SQLite, 실제 편집 적용과 범위 검사. ruff
샌드박스, 테스트 실행, GitHub, 푸시만 가짜다. Docker 없음. 실제 ruff로 끝까지 도는 것은
tests/e2e/test_lint_cases.py가 본다.
"""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import pytest

from app.shared import state_keys_pure
from app.shared.job_pure import PRJob
from app.worker import base_config, main, repo, retry
from app.worker.ai import claude_cli
from app.worker.checker_pure import CheckerKind, tool_codes
from app.worker.doc_rules_pure import Found, NoRuleDocs, RuleChecked
from app.worker.fix import proposal
from app.worker.github import GitHubError
from app.worker.rules import doc_rules
from app.worker.sandbox.checker_runner import CheckerRun, Match, Ran
from app.worker.test_run_pure import PytestRun, ReportSummary
from app.worker.text_edit_pure import TextEdit

from ..helpers import PASSED, bodies, use_runner
from ..rules.test_doc_rules_flow import make_remote
from ..test_github import is_reply

REAL_CHECKOUT: Final = repo.checkout
REAL_ENTRIES: Final = repo.list_base_entries
REAL_LOAD: Final = base_config.load
BRANCH: Final = "pipeline/pr-1-fix-mechanical"
BASE_FILES: Final = {"ruff.toml": "", "a.py": "x = 1\n"}
PR_FILES: Final = {"a.py": "import sys\nx = 1\nl = 2\n"}  # F401(고칠 수 있음), E741(없음)
APPROVAL: Final = {".pipeline.yaml": "fix:\n  require_approval: true\n"}


class FakeRuff:
    """ruff 대역. `import sys`/`import os` 줄은 F401(줄을 지우는 안전한 수정), `l = ` 줄은 E741
    (수정 없음). 포맷은 파일 앞의 빈 줄을 지운다. 받은 호출을 남긴다."""

    def __init__(self) -> None:
        self.base_formatted: bool | str = True
        self.extra: list[Match] = []  # 다음 검사부터 더할 위반 (새 위반 흉내)
        self.calls: list[tuple[str, list[str]]] = []

    def on_examples(self, kind: CheckerKind, body: str, snippets: Sequence[str]) -> CheckerRun:
        raise AssertionError("이 테스트에는 문서 규칙이 없다")

    def on_files(
        self, kind: CheckerKind, body: str, root: Path, files: Sequence[str]
    ) -> CheckerRun:
        assert kind == "ruff"
        self.calls.append(("check", list(files)))
        codes = tool_codes(body)
        matches = []
        for f in files:
            for n, line in enumerate((root / f).read_text().split("\n"), 1):
                if line.startswith(("import sys", "import os")):
                    edit = TextEdit(n, 1, n + 1, 1, "")
                    matches.append(Match(f, n, n, f"`{line[7:]}` imported but unused", "F401",
                                         (edit,)))  # fmt: skip
                elif line.startswith("l = "):
                    matches.append(Match(f, n, n, "Ambiguous variable name: `l`", "E741"))
        matches += [m for m in self.extra if m.file in files]
        return Ran(tuple(m for m in matches if not codes or m.code in codes))

    def rule_exists(self, code: str) -> bool:
        return True

    def format_check(self, root: Path) -> bool | str:
        self.calls.append(("format_check", []))
        return self.base_formatted

    def format_apply(self, root: Path, files: Sequence[str]) -> bool | str:
        self.calls.append(("format", list(files)))
        for f in files:
            (root / f).write_text((root / f).read_text().lstrip("\n"))
        return True


def _no_model(*args, **kwargs):
    pytest.fail("도구 설정의 규칙과 기계적 수정은 모델을 부르지 않는다")


@pytest.fixture
def mech_env(env, tmp_path: Path):
    mp = env["monkeypatch"]
    mp.setattr(repo, "checkout", REAL_CHECKOUT)
    mp.setattr(repo, "list_base_entries", REAL_ENTRIES)
    mp.setattr(base_config, "load", REAL_LOAD)
    mp.setattr(repo, "remote_url", lambda name: str(tmp_path / "remote"))
    ruff = FakeRuff()
    mp.setattr(doc_rules, "sandbox_for", lambda *a: ruff)
    mp.setattr(claude_cli, "run_structured", _no_model)
    mp.setattr(claude_cli, "run_claude", _no_model)
    use_runner(env, lambda *a, **k: PASSED)
    pushes: list[dict] = []

    def fake_push(worktree, paths, message, branch, author):
        pushes.append({"paths": list(paths), "branch": branch, "author": author,
                       "diff": repo.head_diff(worktree)})  # fmt: skip
        return "f" * 40

    mp.setattr(repo, "commit_and_push", fake_push)
    env.update(ruff=ruff, pushes=pushes, remote=tmp_path / "remote")
    return env


def _open(env, base: dict[str, str] = BASE_FILES, pr: dict[str, str] = PR_FILES) -> str:
    base_sha, head = make_remote(env["remote"], base, pr)
    env["gh"].head, env["gh"].base = head, base_sha
    env["redis"].set(state_keys_pure.latest_head("o/r", 1), head)
    return head


def _update(env, head: str | None = None) -> str:
    job = PRJob(repo="o/r", pr_number=1, kind="update", head_sha=head or env["gh"].head,
                base_sha=env["gh"].base, source="webhook")  # fmt: skip
    return main.handle(job.model_dump_json(), env["deps"])


def _command(env, text: str = "/fix mechanical", comment_id: int = 77) -> str:
    job = PRJob(repo="o/r", pr_number=1, kind="command", command=text, comment_id=comment_id,
                source="webhook")  # fmt: skip
    return main.handle(job.model_dump_json(), env["deps"])


def _push_commit(env, files: dict[str, str]) -> str:
    """원본 PR의 브랜치에 새 커밋을 올린다."""
    import subprocess

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
            cwd=env["remote"], check=True, capture_output=True, text=True,
        ).stdout.strip()  # fmt: skip

    git("checkout", "-q", env["gh"].head)
    for name, text in files.items():
        (env["remote"] / name).write_text(text)
    git("commit", "-qam", "next")
    head = git("rev-parse", "HEAD")
    git("update-ref", "refs/pull/1/head", head)
    env["gh"].head = head
    env["redis"].set(state_keys_pure.latest_head("o/r", 1), head)
    return head


def _comment(env) -> str:
    return [b for b in bodies(env["gh"]) if "### pr-proof" in b][-1]


def _replies(gh) -> list[str]:
    return [json.loads(stdin)["body"] for _, stdin in gh.calls if is_reply(stdin)]


def test_ruff_settings_become_a_rule_set_and_fixable_violations_go_to_a_fix_pr(mech_env) -> None:
    """ruff 설정이 있는 base: 도구 설정의 규칙 묶음이 생기고, ruff 코드가 규칙 id인 위반이 나온다.
    안전한 수정이 있는 F401은 기계적 수정으로 고쳐 수정안 PR로, 없는 E741은 알리기만 한다."""
    _open(mech_env)
    assert _update(mech_env) == "passed"
    body = _comment(mech_env)
    lines = body.splitlines()
    top = lines.index("**테스트:** 통과 (9개)")
    assert lines[top + 1] == "**규칙:** ⚠️ 위반 2건 (규칙 2개) · 수정안 #900", body
    assert "- `F401` `ruff.toml의 설정대로 ruff를 통과한다` · 1건" in body
    assert "출처: `ruff.toml` · 도구 `ruff` (`ruff.toml`의 설정)" in body
    fixed = "`a.py:1` `` `sys` imported but unused `` · 고칠 대상 (기계적 수정) · 수정안 #900\n"
    assert fixed in body
    unfixable = "  - `a.py:3` `` Ambiguous variable name: `l` `` · 도구의 자동 수정이 없음"
    assert unfixable in body
    assert "**기계적 수정안:** 위반 1건을 고쳤습니다" in body
    assert "수정안 PR #900을 열었습니다." in body
    (push,) = mech_env["pushes"]
    assert push["branch"] == BRANCH and push["paths"] == ["a.py"]
    assert "-import sys" in push["diff"] and "-l = 2" not in push["diff"]
    pull = mech_env["gh"].pulls[900]
    assert (pull["base"], pull["head"]) == ("feature", BRANCH)
    assert pull["title"] == "[pipeline] #1 위반 1건 수정 (기계적 수정)"
    assert "- `F401` `ruff.toml의 설정대로 ruff를 통과한다` · 출처: `ruff.toml`" in pull["body"]
    assert "도구: ruff 0.17.5 (기본 버전)" in pull["body"]
    (record,) = mech_env["deps"].store.proposals("o/r", 1)
    assert (record.skill, record.state, record.attempt_id) == ("mechanical", "open", 1)
    assert mech_env["deps"].store.fix_attempt(1).bundle == "mechanical"


def test_no_ruff_settings_and_no_rule_docs_means_no_rule_check(mech_env) -> None:
    """ruff 설정도 규칙 문서도 없는 base는 규칙 검사를 하지 않고 그 사실을 적는다 (3-3, 2.2).
    ruff를 부르지 않는다."""
    _open(mech_env, base={"a.py": "x = 1\n"})
    assert _update(mech_env) == "passed"
    body = _comment(mech_env)
    assert "**규칙:** 규칙을 찾지 못해 규칙 검사를 하지 않음" in body
    assert "ruff 설정(`ruff.toml`" in body  # 규칙을 두는 방법을 알린다
    assert mech_env["ruff"].calls == [] and mech_env["pushes"] == []


def test_a_ruff_code_can_be_disabled(mech_env) -> None:
    """`rules.disabled`에 ruff 코드를 적으면 도구 설정 규칙의 그 코드 위반만 빠진다 (10절)."""
    _open(mech_env, base={**BASE_FILES, ".pipeline.yaml": "rules:\n  disabled: [E741]\n"})
    _update(mech_env)
    body = _comment(mech_env)
    assert "`F401`" in body and "E741" not in body


def test_the_rule_set_can_be_disabled_by_its_id(mech_env) -> None:
    _open(mech_env, base={**BASE_FILES, ".pipeline.yaml": "rules:\n  disabled: [ruff]\n"})
    _update(mech_env)
    assert "**규칙:** 규칙을 찾지 못해 규칙 검사를 하지 않음" in _comment(mech_env)
    assert mech_env["ruff"].calls == []


def test_old_skills_enabled_without_lint_turns_the_rule_set_off_with_a_note(mech_env) -> None:
    _open(mech_env, base={**BASE_FILES, ".pipeline.yaml": "skills:\n  enabled: []\n"})
    _update(mech_env)
    body = _comment(mech_env)
    assert "**규칙:** 규칙을 찾지 못해 규칙 검사를 하지 않음" in body
    assert "`skills.enabled`에 `lint`가 없어 도구 설정의 ruff 규칙(`ruff`)을 끕니다" in body


def test_pr_cannot_change_the_ruff_settings_for_itself(mech_env) -> None:
    """PR이 ruff 설정을 바꿔도 base의 설정으로 검사한다 (T13)."""
    _open(mech_env, pr={**PR_FILES, "ruff.toml": "[lint]\nignore = ['F401']\n"})
    _update(mech_env)
    assert "`F401`" in _comment(mech_env)


def test_symlinked_ruff_settings_are_not_read(mech_env) -> None:
    """base의 설정 파일이 심볼릭 링크면 읽지 않고 "설정:"에 알린다 (T24)."""
    remote = mech_env["remote"]
    _open(mech_env, base={"a.py": "x = 1\n", "real.toml": ""})
    import os
    import subprocess

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
            cwd=remote, check=True, capture_output=True, text=True,
        ).stdout.strip()  # fmt: skip

    git("checkout", "-q", "-b", "linked", mech_env["gh"].base)
    os.symlink("real.toml", remote / "ruff.toml")
    git("add", "-A")
    git("commit", "-qm", "link")
    base = git("rev-parse", "HEAD")
    git("checkout", "-q", mech_env["gh"].head)
    mech_env["gh"].base = base
    _update(mech_env)
    body = _comment(mech_env)
    assert "`ruff.toml`이 일반 파일이 아니라 읽지 않음" in body
    assert "**규칙:** 규칙을 찾지 못해 규칙 검사를 하지 않음" in body


def test_approval_waits_and_fix_mechanical_resumes(mech_env) -> None:
    _open(mech_env, base={**BASE_FILES, **APPROVAL})
    _update(mech_env)
    body = _comment(mech_env)
    assert "`/fix mechanical`을 달면 만듭니다." in body and mech_env["pushes"] == []
    assert _command(mech_env) == "fix:ready"
    assert len(mech_env["pushes"]) == 1 and "수정안 PR #900을 열었습니다." in _comment(mech_env)
    assert _command(mech_env, comment_id=78) == "no_pending_fix"
    assert "mechanical 제안 PR #900이 이미 열려 있습니다" in _replies(mech_env["gh"])[-1]


def test_plain_fix_approves_every_waiting_bundle(mech_env) -> None:
    _open(mech_env, base={**BASE_FILES, **APPROVAL})
    _update(mech_env)
    assert _command(mech_env, "/fix") == "fix:ready"


@pytest.mark.parametrize("text", ["/fix lint", "/fix static"])
def test_v1_skill_names_are_not_supported(mech_env, text: str) -> None:
    """v1의 `/fix lint`는 3단계에 기계적 수정 묶음이 되었다. 옛 이름은 지원하지 않는다고 답한다."""
    _open(mech_env, base={**BASE_FILES, **APPROVAL})
    _update(mech_env)
    assert _command(mech_env, text) == "fix_unsupported"
    reply = _replies(mech_env["gh"])[-1]
    assert f"`{text}`는 지원하지 않습니다" in reply and "`/fix mechanical`" in reply
    assert mech_env["pushes"] == []


def test_failing_tests_block_the_fix(mech_env) -> None:
    _open(mech_env)
    failed = PytestRun(exit_code=1, summary=ReportSummary(passed=8, failed=1, total=9))
    use_runner(mech_env, lambda *a, **k: failed)
    assert _update(mech_env) == "failed"
    body = _comment(mech_env)
    assert "  직접 고쳐야 함 1건 (테스트가 실패함 1건)" in body and "**기계적 수정안:**" not in body
    assert mech_env["pushes"] == []


def test_new_violation_after_the_fix_discards_it(mech_env) -> None:
    _open(mech_env)
    real = mech_env["ruff"].on_files

    def with_new(kind, body, root, files):
        run = real(kind, body, root, files)
        if not (root / "a.py").read_text().startswith("import sys"):  # 고친 뒤
            return Ran((*run.matches, Match("a.py", 2, 2, "new", "F811")))
        return run

    mech_env["ruff"].on_files = with_new
    _update(mech_env)
    body = _comment(mech_env)
    assert "수정안을 버림: 고친 뒤 원래 없던 위반이 생김" in body and mech_env["pushes"] == []
    assert mech_env["deps"].store.fix_attempt(1).status == "new_violations"


def test_tests_changing_after_the_fix_discards_it(mech_env) -> None:
    _open(mech_env)
    runs = iter([PASSED, PytestRun(exit_code=0, summary=ReportSummary(passed=8, total=8))])
    use_runner(mech_env, lambda *a, **k: next(runs))
    _update(mech_env)
    assert "수정안을 버림: 고친 뒤 테스트 결과가 달라짐" in _comment(mech_env)
    assert mech_env["pushes"] == []


def test_format_is_applied_only_when_base_is_formatted(mech_env) -> None:
    pr = {"a.py": "import sys\n\nx = 1\n"}  # 고치면 빈 줄로 시작한다. 포맷이 지운다
    _open(mech_env, pr=pr)
    _update(mech_env)
    (push,) = mech_env["pushes"]
    assert "`ruff format`을 고친 파일 1개에 적용했습니다." in _comment(mech_env)
    assert ("format", ["a.py"]) in mech_env["ruff"].calls


def test_format_skip_reason_is_shown(mech_env) -> None:
    mech_env["ruff"].base_formatted = False
    _open(mech_env)
    _update(mech_env)
    body = _comment(mech_env)
    assert "`ruff format`은 적용하지 않았습니다 (`base 브랜치가 ruff format을 따르지 않음`)" in body
    assert not any(c[0] == "format" for c in mech_env["ruff"].calls)


def test_fork_pr_gets_no_fix_pr_but_the_patch_is_shown(mech_env) -> None:
    mech_env["gh"].head_repo = "someone/r"
    _open(mech_env)
    _update(mech_env)
    body = _comment(mech_env)
    assert mech_env["pushes"] == [] and mech_env["gh"].pulls == {}
    assert "수정안 PR을 만들지 않았습니다 (포크에서 온 PR이라" in body
    assert "<summary>수정 내용</summary>" in body and "-import sys" in body


def test_secret_on_a_fixed_line_drops_the_fix(mech_env, notifications) -> None:
    token = "ghp_" + "a" * 36
    _open(mech_env, pr={"a.py": f"import sys; T = '{token}'\nx = 1\n"})
    real = mech_env["ruff"].on_files

    def secret_edit(kind, body, root, files):
        run = real(kind, body, root, files)
        text = (root / "a.py").read_text()
        if not text.startswith("import sys;"):
            return run
        edit = TextEdit(1, 1, 1, 12, "")  # `import sys; `만 지운다. 토큰이 남은 줄이 바뀐 줄이 된다
        return Ran((Match("a.py", 1, 1, "unused", "F401", (edit,)),))

    mech_env["ruff"].on_files = secret_edit
    _update(mech_env)
    body = _comment(mech_env)
    assert mech_env["pushes"] == [] and "비밀값으로 보이는 내용이 있음" in body
    # 위협 T21: 결과 코멘트와 로그에 남는다. 사람이 할 환경 오류가 아니라 알림은 없다 (6-4)
    assert notifications == []
    assert token not in body.split("**기계적 수정안:**")[1]


def test_failed_delivery_is_retried_with_the_stored_patch(mech_env) -> None:
    """수정안 PR을 만들다 실패하면 다음 시도가 `deliver_mechanical`부터 이어 간다. 고치지 않고
    저장한 패치를 올린다 (3.1의 C6)."""
    _open(mech_env)
    real_open = proposal.open_mechanical_proposal
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise GitHubError("pulls 생성 실패")
        return real_open(*a, **k)

    mech_env["monkeypatch"].setattr(proposal, "open_mechanical_proposal", flaky)
    mech_env["monkeypatch"].setattr(retry, "attempt", lambda: (1, False))
    with pytest.raises(GitHubError):
        _update(mech_env)
    checks = len([c for c in mech_env["ruff"].calls if c[0] == "check"])
    mech_env["monkeypatch"].setattr(retry, "attempt", lambda: (2, False))
    assert _update(mech_env) == "passed"
    assert len([c for c in mech_env["ruff"].calls if c[0] == "check"]) == checks  # 다시 고치지 않음
    assert len(mech_env["gh"].pulls) == 1 and len(mech_env["pushes"]) == 1


def test_new_commit_supersedes_and_merge_commit_does_not_loop(mech_env) -> None:
    _open(mech_env)
    _update(mech_env)
    _push_commit(mech_env, {"a.py": "import sys\nx = 2\nl = 2\n"})
    _update(mech_env)
    assert mech_env["gh"].pulls[900]["state"] == "closed"
    assert mech_env["gh"].deleted_branches == [BRANCH]
    states = [p.state for p in mech_env["deps"].store.proposals("o/r", 1)]
    assert states == ["superseded", "open"]
    merge = _push_commit(mech_env, {"a.py": "x = 2\nl = 2\n"})
    mech_env["gh"].pulls[901].update(state="closed", merged=True, merge_commit=merge)
    _update(mech_env)
    assert len(mech_env["gh"].pulls) == 2


def test_rejected_fix_pr_is_not_made_again_and_its_line_is_skipped_next_commit(mech_env) -> None:
    """사람이 닫은 기계적 수정안: 그 수정안이 고친 줄은 같은 커밋의 `/recheck`에서도, 다음
    커밋에서도 `skip`(이전에 거절됨)이다. 거절은 `records`에 남는다 (4.6, 4.10)."""
    _open(mech_env)
    _update(mech_env)
    mech_env["gh"].pulls[900]["state"] = "closed"
    assert _command(mech_env, "/recheck", 80).startswith("recheck:")
    assert " · 이전에 거절됨 (#900)" in _comment(mech_env)
    assert mech_env["deps"].store.records("o/r", 1)[0].kind == "rejected"
    _push_commit(mech_env, {"a.py": "import sys\nx = 3\nl = 2\n"})
    _update(mech_env)
    assert " · 이전에 거절됨 (#900)" in _comment(mech_env)
    assert len(mech_env["gh"].pulls) == 1


def test_closing_the_original_pr_closes_the_fix_pr(mech_env) -> None:
    _open(mech_env)
    _update(mech_env)
    mech_env["gh"].state = "closed"
    close = PRJob(repo="o/r", pr_number=1, kind="close", head_sha=mech_env["gh"].head,
                  base_sha=mech_env["gh"].base, source="webhook")  # fmt: skip
    assert main.handle(close.model_dump_json(), mech_env["deps"]) == "closed:1"
    assert mech_env["deps"].store.proposals("o/r", 1)[0].state == "closed_with_pr"


def test_found_state_names_the_rule_set_and_codes(mech_env) -> None:
    from app.worker import graph, update

    head = _open(mech_env)
    job = PRJob(repo="o/r", pr_number=1, kind="update", head_sha=head,
                base_sha=mech_env["gh"].base, source="webhook")  # fmt: skip
    deps = mech_env["deps"]
    state = graph.run_flow(job, update._context(deps, deps.store.start_run(job), None))
    found = state["doc_rules"]
    assert isinstance(found, Found)
    (outcome,) = found.outcomes
    assert isinstance(outcome, RuleChecked) and outcome.rule.origin == "tool_config"
    assert [(v.rule_id, v.code, v.found_by, v.mechanical_fix) for v in found.violations] == [
        ("F401", "F401", "tool", True),
        ("E741", "E741", "tool", False),
    ]
    assert not isinstance(found, NoRuleDocs)


class SortingRuff(FakeRuff):
    """FakeRuff에 I001을 더한다: 파일 앞의 import 줄들이 정렬돼 있지 않으면 블록 전체를 정렬하는
    안전한 수정."""

    def on_files(self, kind, body, root, files):  # noqa: ANN001, ANN201
        run = super().on_files(kind, body, root, files)
        matches = list(run.matches)
        for f in files:
            lines = (root / f).read_text().split("\n")
            block = [n for n, line in enumerate(lines, 1) if line.startswith("import ")]
            texts = [lines[n - 1] for n in block]
            if block and texts != sorted(texts):
                edit = TextEdit(block[0], 1, block[-1] + 1, 1, "\n".join(sorted(texts)) + "\n")
                matches.append(Match(f, block[0], block[-1], "Import block is un-sorted", "I001",
                                     (edit,)))  # fmt: skip
        return Ran(tuple(matches))


def test_base_import_moved_by_sorting_is_not_a_new_violation(mech_env) -> None:
    """BACKLOG(2단계 코드 검증)의 I001 항목: 정렬이 자리만 옮긴 base의 `import sys`는 다음
    재검사에서 PR이 바꾼 줄이 아니다. 그 줄의 F401(base에 원래 있던 위반)이 "새 위반"으로 잡혀
    수정안이 버려지지 않고, 대상이 아니라 고치지도 않는다."""
    mech_env["ruff"].__class__ = SortingRuff
    _open(mech_env, base={"ruff.toml": "", "a.py": "import sys\nimport os\nx = 1\n"},
          pr={"a.py": "import abc\nimport sys\nimport os\nx = 1\n"})  # fmt: skip
    assert _update(mech_env) == "passed"
    body = _comment(mech_env)
    assert "**기계적 수정안:** 위반 1건을 고쳤습니다" in body, body
    (push,) = mech_env["pushes"]
    assert "import os" in push["diff"] and "import sys" in push["diff"]
    assert "-import os\n" not in push["diff"].split("@@")[-1].replace("+import os\n", "")
