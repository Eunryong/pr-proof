"""문서 규칙 경로가 그래프에서 도는지 (docs/DESIGN.md 5.1, 13절 1단계).

실제 git 체크아웃(로컬 원격)과 실제 SQLite로 돈다. 모델은 부르지 않는다. Docker 없음.
"""

import subprocess
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Final

import pytest
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.shared import state_keys_pure
from app.shared.job_pure import PRJob
from app.worker import base_config, graph, repo, update
from app.worker.ai import claude_cli
from app.worker.core.checker_pure import CheckerKind
from app.worker.core.rule_pure import CheckMethod, Rule, RuleSource
from app.worker.doc_rules_pure import CallUsage, Found, NoRuleDocs, NotRun, RuleUnchecked
from app.worker.fix.fix_decision_pure import Decided, FixDecision
from app.worker.repo_config_pure import CallSettings, ModelSettings, RepoConfig
from app.worker.rules import doc_rules
from app.worker.sandbox import checker_runner, toolcache
from app.worker.sandbox.checker_runner import CheckerRun, Match, Ran

from ..helpers import BASE, PASSED, bodies, use_runner

REAL_CHECKOUT: Final = repo.checkout
REAL_ENTRIES: Final = repo.list_base_entries
REAL_SANDBOX_FOR: Final = doc_rules.sandbox_for
PRINT_RULE: Final = "`orders/` 아래 모듈은 `print`를 쓰지 않는다."
README: Final = f"# demo\n\n## 코드 규칙\n\n### 출력\n\n- {PRINT_RULE}\n"
SECTION: Final = "코드 규칙 > 출력"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip


def make_remote(path: Path, base: dict[str, str], pr: dict[str, str]) -> tuple[str, str]:
    """base 커밋과 PR 커밋(refs/pull/1/head)이 있는 로컬 원격. (base, head) SHA."""
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "uploadpack.allowAnySHA1InWant", "true")
    for files in (base, pr):
        for name, text in files.items():
            (path / name).parent.mkdir(parents=True, exist_ok=True)
            (path / name).write_text(text)
        _git(path, "add", "-A")
        _git(path, "commit", "-q", "--allow-empty", "-m", "c")
    head = _git(path, "rev-parse", "HEAD")
    base_sha = _git(path, "rev-parse", "HEAD^")
    _git(path, "update-ref", "refs/pull/1/head", head)
    return base_sha, head


class GrepSandbox:
    """본문이 `grep:<글>`이면 그 글이 든 줄을 잡는 가짜 검사기 샌드박스."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.files: list[list[str]] = []

    def on_examples(self, kind: CheckerKind, body: str, snippets: Sequence[str]) -> CheckerRun:
        return self._grep(
            body, [(checker_runner.example_file(kind, i, s), s) for i, s in enumerate(snippets)]
        )

    def on_files(
        self, kind: CheckerKind, body: str, root: Path, files: Sequence[str]
    ) -> CheckerRun:
        if self.error is not None:
            raise self.error
        self.files.append(list(files))
        if kind == "path-pattern":  # 경로 규칙은 내용이 아니라 경로를 본다
            return self._grep(body, [(f, f) for f in files])
        return self._grep(body, [(f, (root / f).read_text()) for f in files])

    @staticmethod
    def _grep(body: str, sources: list[tuple[str, str]]) -> CheckerRun:
        needle = body.strip().removeprefix("grep:")
        return Ran(tuple(
            Match(name, n, n, "m")
            for name, text in sources for n, line in enumerate(text.split("\n"), 1)
            if needle in line
        ))  # fmt: skip


@pytest.fixture
def doc_env(env, tmp_path: Path):
    """env에 실제 체크아웃과 가짜 검사기 샌드박스를 붙인다. 모델은 부르지 않는다."""
    mp = env["monkeypatch"]
    mp.setattr(repo, "checkout", REAL_CHECKOUT)
    mp.setattr(repo, "list_base_entries", REAL_ENTRIES)
    mp.setattr(repo, "remote_url", lambda name: str(tmp_path / "remote"))
    sandbox = GrepSandbox()
    mp.setattr(doc_rules, "sandbox_for", lambda *a: sandbox)
    use_runner(env, lambda *a, **k: PASSED)
    env["sandbox"] = sandbox
    mp.setattr(claude_cli, "run_claude", _no_model)
    mp.setattr(claude_cli, "run_structured", _no_model)
    return env


def _no_model(*args, **kwargs):
    """실제 claude를 부르지 않는다. 모델을 부르는 흐름은 eval/과 실제 모델 테스트가 본다."""
    pytest.fail("이 테스트에서는 모델을 부르지 않아야 한다")


def use_config(env, config: RepoConfig) -> None:
    # PR 설명 확인은 BASE처럼 꺼 둔다 (모델을 부르지 않게)
    config = config.model_copy(update={"pr_description": BASE.repo.pr_description})
    env["monkeypatch"].setattr(base_config, "load", lambda *a, **k: replace(BASE, repo=config))


def check(env, tmp_path: Path, base: dict[str, str], pr: dict[str, str], pr_number: int = 1):
    """PR(pr_number)의 한 커밋을 그래프로 돌린 상태."""
    remote = tmp_path / "remote"
    if remote.exists():
        _git(remote, "checkout", "-q", "--orphan", f"x{pr_number}")
        _git(remote, "rm", "-rqf", "--ignore-unmatch", ".")
        base_sha = _commit(remote, base)
        head = _commit(remote, pr)
        _git(remote, "update-ref", f"refs/pull/{pr_number}/head", head)
    else:
        base_sha, head = make_remote(remote, base, pr)
    deps = env["deps"]
    env["redis"].set(state_keys_pure.latest_head("o/r", pr_number), head)
    job = PRJob(repo="o/r", pr_number=pr_number, kind="update", head_sha=head, base_sha=base_sha,
                source="webhook")  # fmt: skip
    ctx = update.flow_context(deps, deps.store.start_run(job), None)
    return graph.run_flow(job, ctx)


def _commit(path: Path, files: dict[str, str]) -> str:
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "--allow-empty", "-m", "c")
    return _git(path, "rev-parse", "HEAD")


BASE_FILES: Final = {"README.md": README, "orders/a.py": "x = 1\n", "scripts/b.py": "y = 1\n"}
PR_FILES: Final = {"orders/a.py": "x = 1\nprint(x)\n", "scripts/b.py": "y = 1\nprint(y)\n"}


def test_base_without_rule_documents(doc_env, tmp_path: Path) -> None:
    state = check(doc_env, tmp_path, {"orders/a.py": "x = 1\n"}, PR_FILES)
    assert state["doc_rules"] == NoRuleDocs(())
    assert state["test_run"] == PASSED


def test_repo_daily_cap_stops_model_stages_but_not_tests(doc_env, tmp_path: Path) -> None:
    use_config(doc_env, RepoConfig(model=ModelSettings(calls=CallSettings(repo_daily=0))))
    state = check(doc_env, tmp_path, BASE_FILES, PR_FILES)
    assert isinstance(state["doc_rules"], NotRun)
    assert "저장소 하루 상한" in state["doc_rules"].reason
    assert state["test_run"] == PASSED


def test_decisions_survive_checkpoint() -> None:
    decided = Decided((FixDecision("v1", "skip", "model", reason="risky", why="w"),), calls=1)
    serde = JsonPlusSerializer(allowed_msgpack_modules=list(graph._STATE_TYPES))
    restored = serde.loads_typed(serde.dumps_typed({"d": decided}))["d"]
    (decision,) = restored.decisions
    assert isinstance(decision, FixDecision) and decision.reason == "risky" and restored.calls == 1


def test_doc_rules_state_survives_checkpoint() -> None:
    rule = Rule(id="R-readme-1", text="t", source=RuleSource("README.md", SECTION, "x"),
                origin="document", scope=("orders/**",), check=CheckMethod("checker"),
                fix="model", exclude=("tests/**",), exclude_reason="테스트 코드")  # fmt: skip
    found = Found(outcomes=(RuleUnchecked(rule, "no_checker", "d"),), violations=(),
                  disabled=0, usage=CallUsage(0, 10, 0, (), 0, 0))  # fmt: skip
    serde = JsonPlusSerializer(allowed_msgpack_modules=list(graph._STATE_TYPES))
    restored = serde.loads_typed(serde.dumps_typed({"doc_rules": found}))["doc_rules"]
    (outcome,) = restored.outcomes
    assert isinstance(outcome.rule.source, RuleSource) and outcome.rule.exclude == ["tests/**"]


def test_comment_for_base_without_rules_explains_how_to_write_them(doc_env, tmp_path) -> None:
    check(doc_env, tmp_path, {"orders/a.py": "x = 1\n"}, PR_FILES)
    body = bodies(doc_env["gh"])[-1]
    assert "**규칙:** 규칙을 찾지 못해 규칙 검사를 하지 않음" in body
    assert "`CONTRIBUTING.md`에 팀 규칙을 적어 두면 다음 PR부터" in body


def test_ast_grep_is_not_fetched_without_rule_documents(doc_env, tmp_path: Path) -> None:
    doc_env["monkeypatch"].setattr(doc_rules, "sandbox_for", REAL_SANDBOX_FOR)
    doc_env["monkeypatch"].setattr(
        toolcache, "ensure", lambda *a, **k: pytest.fail("ast-grep을 받으면 안 된다")
    )
    state = check(doc_env, tmp_path, {"orders/a.py": "x = 1\n"}, PR_FILES)
    assert not isinstance(state["doc_rules"], NotRun)
