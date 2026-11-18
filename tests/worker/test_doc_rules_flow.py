"""문서 규칙 경로가 그래프에서 도는지 (docs/DESIGN.md 5.1, 13절 1단계).

실제 git 체크아웃(로컬 원격)과 실제 SQLite, 가짜 모델과 가짜 검사기 샌드박스로 돈다. Docker 없음.
"""

import subprocess
from collections.abc import Sequence
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Final

import pytest
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from sqlmodel import Session, select

from app.models import rule_reading, state_keys
from app.models.checker import CheckerKind
from app.models.db import ModelCall
from app.models.doc_rules import (
    CallUsage,
    Found,
    NoRuleDocs,
    NotRun,
    RuleChecked,
    RuleUnchecked,
)
from app.models.job import PRJob
from app.models.repo_config import CallSettings, ModelSettings, RepoConfig, RulesSettings
from app.models.rule import CheckMethod, Rule, RuleSource, rule_id
from app.sandbox import checker_runner, toolcache
from app.sandbox.checker_runner import CheckerRun, Match, Ran
from app.sandbox.docker_cli import SandboxError
from app.sandbox.venv import DependencyInstallError
from app.worker import base_config, doc_rules, graph, repo, update
from app.worker.ai import claude_cli
from app.worker.ai.claude_cli import AIErrorKind, StructuredResult

from .helpers import BASE, PASSED, bodies, use_runner

REAL_CHECKOUT: Final = repo.checkout
REAL_ENTRIES: Final = repo.list_base_entries
REAL_SANDBOX_FOR: Final = doc_rules.sandbox_for
PRINT_RULE: Final = "`orders/` 아래 모듈은 `print`를 쓰지 않는다."
README: Final = f"# demo\n\n## 코드 규칙\n\n### 출력\n\n- {PRINT_RULE}\n"
SECTION: Final = "코드 규칙 > 출력"
CHECKER_EXAMPLES: Final = [
    {"code": "print(1)\n", "violates": True, "lines": [1]},
    {"code": "def f():\n    print(2)\n", "violates": True, "lines": [2]},
    {"code": "log(1)\n", "violates": False, "lines": []},
    {"code": "logger.info(1)\n", "violates": False, "lines": []},
]


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


def rule_answer(*rules: dict) -> StructuredResult:
    return _ok({"rules": list(rules)})


def print_rule(**fields: object) -> dict:
    rule = {
        "file": "README.md", "section": SECTION, "quote": PRINT_RULE, "scope": ["orders/**"],
        "scope_exclude": [], "scope_reason": "", "examples": [], "check": "checker",
        "reason": "", "fix": "model",
    }  # fmt: skip
    rule.update(fields)
    return rule


def checker_answer(body: str = "grep:print(") -> StructuredResult:
    return _ok({"checker": body, "examples": CHECKER_EXAMPLES})


def _ok(data: dict) -> StructuredResult:
    return StructuredResult(
        ok=True, error=None, data=data, model_id="claude-haiku-5-5", input_tokens=1000,
        output_tokens=100, cost_usd=0.001, permission_denials=0,
    )  # fmt: skip


class FakeModel:
    """규칙 읽기에는 rules, 검사기 작성에는 checker의 답을 준다. 부른 기록을 남긴다."""

    def __init__(self, rules: StructuredResult, checker: StructuredResult | None = None) -> None:
        self.rules, self.checker = rules, checker or checker_answer()
        self.prompts: list[tuple[str, str]] = []

    def __call__(self, prompt: str, *, schema, **kw) -> StructuredResult:
        reading = schema is rule_reading.ANSWER_SCHEMA
        self.prompts.append(("rules" if reading else "checker", prompt))
        return self.rules if reading else self.checker


class GrepSandbox:
    """본문이 `grep:<글>`이면 그 글이 든 줄을 잡는 가짜 검사기 샌드박스."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.files: list[list[str]] = []

    def on_examples(self, kind: CheckerKind, body: str, snippets: Sequence[str]) -> CheckerRun:
        return self._grep(
            body, [(checker_runner.example_path(i), s) for i, s in enumerate(snippets)]
        )

    def on_files(
        self, kind: CheckerKind, body: str, root: Path, files: Sequence[str]
    ) -> CheckerRun:
        if self.error is not None:
            raise self.error
        self.files.append(list(files))
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
    """env에 실제 체크아웃과 가짜 모델·검사기 샌드박스를 붙인다."""
    mp = env["monkeypatch"]
    mp.setattr(repo, "checkout", REAL_CHECKOUT)
    mp.setattr(repo, "list_base_entries", REAL_ENTRIES)
    mp.setattr(repo, "remote_url", lambda name: str(tmp_path / "remote"))
    sandbox = GrepSandbox()
    mp.setattr(doc_rules, "sandbox_for", lambda cache: sandbox)
    use_runner(env, lambda *a, **k: PASSED)
    env["sandbox"] = sandbox
    return env


def use_model(env, model: FakeModel) -> FakeModel:
    env["monkeypatch"].setattr(claude_cli, "run_structured", model)
    return model


def use_config(env, config: RepoConfig) -> None:
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
    env["redis"].set(state_keys.latest_head("o/r", pr_number), head)
    job = PRJob(repo="o/r", pr_number=pr_number, kind="update", head_sha=head, base_sha=base_sha,
                source="webhook")  # fmt: skip
    ctx = update._context(deps, deps.store.start_run(job), None)
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


def test_finds_violation_on_changed_line_inside_scope(doc_env, tmp_path: Path) -> None:
    model = use_model(doc_env, FakeModel(rule_answer(print_rule())))
    state = check(doc_env, tmp_path, BASE_FILES, PR_FILES)
    found = state["doc_rules"]
    assert isinstance(found, Found)
    (outcome,) = found.outcomes
    assert isinstance(outcome, RuleChecked) and outcome.rule.source.section == SECTION
    assert [(v.file, v.line) for v in found.violations] == [("orders/a.py", 2)]
    assert doc_env["sandbox"].files == [["orders/a.py"]]  # 범위 밖 scripts/는 돌리지 않는다
    assert [kind for kind, _ in model.prompts] == ["rules", "checker"]
    assert found.usage == CallUsage(calls=2, limit=14, cache_hits=0,
                                    models=("claude-haiku-5-5",), input_tokens=2000,
                                    output_tokens=200)  # fmt: skip
    assert state["test_run"] == PASSED and state["lint"].findings  # v1 경로와 테스트도 돈다


def test_second_pr_reuses_rules_and_checker_without_calls(doc_env, tmp_path: Path) -> None:
    model = use_model(doc_env, FakeModel(rule_answer(print_rule())))
    check(doc_env, tmp_path, BASE_FILES, PR_FILES)
    state = check(doc_env, tmp_path, BASE_FILES, PR_FILES, pr_number=2)
    found = state["doc_rules"]
    assert len(model.prompts) == 2  # 첫 PR의 규칙 읽기와 검사기 작성뿐
    assert found.usage.calls == 0 and found.usage.cache_hits == 2
    assert [v.file for v in found.violations] == ["orders/a.py"]


def test_rules_come_from_base_even_when_pr_edits_readme(doc_env, tmp_path: Path) -> None:
    model = use_model(doc_env, FakeModel(rule_answer(print_rule())))
    edited = {**PR_FILES, "README.md": README + "- `scripts/`도 `print`를 쓴다.\n"}
    check(doc_env, tmp_path, BASE_FILES, edited)
    (prompt,) = [p for kind, p in model.prompts if kind == "rules"]
    assert PRINT_RULE in prompt and "scripts/`도" not in prompt


def test_base_without_rule_documents(doc_env, tmp_path: Path) -> None:
    model = use_model(doc_env, FakeModel(rule_answer()))
    state = check(doc_env, tmp_path, {"orders/a.py": "x = 1\n"}, PR_FILES)
    assert state["doc_rules"] == NoRuleDocs(())
    assert model.prompts == [] and state["test_run"] == PASSED


def test_model_failure_does_not_fail_the_job(doc_env, tmp_path: Path) -> None:
    failed = StructuredResult(ok=False, error=AIErrorKind.TOOLS, data=None, model_id="",
                              input_tokens=None, output_tokens=None, cost_usd=None,
                              permission_denials=0, detail="도구 목록이 다름")  # fmt: skip
    use_model(doc_env, FakeModel(failed))
    state = check(doc_env, tmp_path, BASE_FILES, PR_FILES)
    assert isinstance(state["doc_rules"], NotRun)
    assert "규칙 읽기의 모델 호출이 실패함" in state["doc_rules"].reason
    assert state["test_run"] == PASSED and state["commented"]


def test_repo_daily_cap_stops_model_stages_but_not_lint(doc_env, tmp_path: Path) -> None:
    model = use_model(doc_env, FakeModel(rule_answer(print_rule())))
    use_config(doc_env, RepoConfig(model=ModelSettings(calls=CallSettings(repo_daily=0))))
    state = check(doc_env, tmp_path, BASE_FILES, PR_FILES)
    assert model.prompts == []
    assert isinstance(state["doc_rules"], NotRun)
    assert "저장소 하루 상한" in state["doc_rules"].reason
    assert state["lint"].findings and state["test_run"] == PASSED


def test_ruleset_cap_leaves_rules_unchecked(doc_env, tmp_path: Path) -> None:
    use_model(doc_env, FakeModel(rule_answer(print_rule())))
    use_config(doc_env, RepoConfig(model=ModelSettings(calls=CallSettings(
        ruleset_base=1, per_rule=0))))  # fmt: skip
    found = check(doc_env, tmp_path, BASE_FILES, PR_FILES)["doc_rules"]
    (outcome,) = found.outcomes
    assert isinstance(outcome, RuleUnchecked) and outcome.kind == "cap"
    assert found.violations == ()


def test_disabled_rule_is_not_applied(doc_env, tmp_path: Path) -> None:
    use_model(doc_env, FakeModel(rule_answer(print_rule())))
    check(doc_env, tmp_path, BASE_FILES, PR_FILES)
    disabled = [rule_id("README.md", PRINT_RULE)]
    use_config(doc_env, RepoConfig(rules=RulesSettings(disabled=disabled)))
    found = check(doc_env, tmp_path, BASE_FILES, PR_FILES, pr_number=2)["doc_rules"]
    assert found.outcomes == () and found.violations == () and found.disabled == 1


def test_sandbox_error_in_checker_run_does_not_fail_the_job(doc_env, tmp_path: Path) -> None:
    use_model(doc_env, FakeModel(rule_answer(print_rule())))
    doc_env["sandbox"].error = SandboxError("docker run 실패")
    state = check(doc_env, tmp_path, BASE_FILES, PR_FILES)
    assert state["doc_rules"] == NotRun("검사기를 돌리는 샌드박스가 실패함: docker run 실패")
    assert state["test_run"] == PASSED and state["commented"]


def test_new_commit_during_rule_reading_stops_the_path(doc_env, tmp_path: Path) -> None:
    model = FakeModel(rule_answer(print_rule()))

    def newer_commit_arrives(prompt: str, **kw) -> StructuredResult:
        doc_env["redis"].set(state_keys.latest_head("o/r", 1), "f" * 40)
        return model(prompt, **kw)

    use_model(doc_env, newer_commit_arrives)
    state = check(doc_env, tmp_path, BASE_FILES, PR_FILES)
    assert state["stopped"] == "stale"
    assert "rule_checks" not in state and "test_run" not in state
    assert [kind for kind, _ in model.prompts] == ["rules"]


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


def _last_comment(env) -> str:
    return bodies(env["gh"])[-1]


def test_comment_shows_rules_violations_and_checker(doc_env, tmp_path: Path) -> None:
    use_model(doc_env, FakeModel(rule_answer(print_rule())))
    check(doc_env, tmp_path, BASE_FILES, PR_FILES)
    body = _last_comment(doc_env)
    assert "**문서 규칙:** ⚠️ 위반 1건 (규칙 1개)" in body
    rid = rule_id("README.md", PRINT_RULE)
    assert f"- `orders/a.py:2` `{rid}` `` {PRINT_RULE} ``" in body
    assert "  출처: `README.md > 코드 규칙 > 출력` · 검사기 `" in body
    assert (
        "<details><summary>모델이 쓴 검사기 <code>" in body and "```yaml\ngrep:print(\n```" in body
    )
    assert "예시: 문서 0개, 모델이 쓴 것 4개" in body and "모델이 쓴 예시, 위반 (줄 1):" in body
    assert "<details><summary>읽은 규칙 1개</summary>" in body
    assert "모델 호출 2회 / 상한 14회 (규칙 읽기와 검사기 작성)" in body
    assert "`rules.disabled`에 규칙 id를 적으세요." in body
    assert "**규칙:** ⚠️ 위반 1건 (lint 1)" in body  # v1 절은 그대로


def test_comment_for_base_without_rules_explains_how_to_write_them(doc_env, tmp_path) -> None:
    use_model(doc_env, FakeModel(rule_answer()))
    check(doc_env, tmp_path, {"orders/a.py": "x = 1\n"}, PR_FILES)
    body = _last_comment(doc_env)
    assert "**문서 규칙:** 규칙을 찾지 못해 규칙 검사를 하지 않음" in body
    assert "`## 코드 규칙` 같은 절을 두고 규칙을 한 문장씩 적으면" in body


THREE_RULES: Final = (
    "- `orders/` 아래 모듈은 `print`를 쓰지 않는다.\n"
    "- `orders/` 아래 모듈은 `pprint`도 쓰지 않는다.\n"
    "- `orders/` 아래 모듈은 `breakpoint`를 남기지 않는다.\n"
)


def _age_model_calls(env, hours: int) -> None:
    with Session(env["deps"].store.engine) as session:
        for row in session.exec(select(ModelCall)):
            row.created_at -= timedelta(hours=hours)
            session.add(row)
        session.commit()


def test_ruleset_cap_stops_authoring_and_next_check_continues(doc_env, tmp_path: Path) -> None:
    quotes = [line[2:] for line in THREE_RULES.splitlines()]
    model = use_model(doc_env, FakeModel(rule_answer(*(print_rule(quote=q) for q in quotes))))
    readme = f"# demo\n\n## 코드 규칙\n\n### 출력\n\n{THREE_RULES}"
    use_config(doc_env, RepoConfig(model=ModelSettings(calls=CallSettings(ruleset_max=3))))
    found = check(doc_env, tmp_path, {**BASE_FILES, "README.md": readme}, PR_FILES)["doc_rules"]
    assert [o.kind for o in found.outcomes if isinstance(o, RuleUnchecked)] == ["cap"]
    assert "모델 호출 상한에 걸려 처리하지 못함: 검사기 작성 1개 규칙" in _last_comment(doc_env)
    assert "남은 규칙부터 이어서 합니다" in _last_comment(doc_env)

    # 같은 날의 다음 검사는 같은 상한에 다시 걸린다
    found = check(doc_env, tmp_path, {**BASE_FILES, "README.md": readme}, PR_FILES, pr_number=3)[
        "doc_rules"
    ]
    assert [o.kind for o in found.outcomes if isinstance(o, RuleUnchecked)] == ["cap"]
    assert [kind for kind, _ in model.prompts] == ["rules", "checker", "checker"]

    # 24시간이 지나면 상한을 올리지 않아도 남은 규칙만 작성한다 (작성한 둘은 저장된 것을 쓴다)
    _age_model_calls(doc_env, hours=25)
    found = check(doc_env, tmp_path, {**BASE_FILES, "README.md": readme}, PR_FILES, pr_number=2)[
        "doc_rules"
    ]
    assert all(isinstance(o, RuleChecked) for o in found.outcomes)
    assert [kind for kind, _ in model.prompts] == ["rules", "checker", "checker", "checker"]
    assert found.usage.calls == 1 and found.usage.cache_hits == 3


def test_source_only_dependency_makes_tests_unrunnable_and_keeps_rules(doc_env, tmp_path) -> None:
    use_model(doc_env, FakeModel(rule_answer(print_rule())))

    def install_fails(*a, **k):
        raise DependencyInstallError("source_only", "key", "ERROR: No matching …", ("docopt",))

    use_runner(doc_env, install_fails)
    remote = tmp_path / "remote"
    base_sha, head = make_remote(remote, BASE_FILES, PR_FILES)
    doc_env["gh"].head, doc_env["gh"].base = head, base_sha
    doc_env["redis"].set(state_keys.latest_head("o/r", 1), head)
    job = PRJob(repo="o/r", pr_number=1, kind="update", head_sha=head, base_sha=base_sha,
                source="webhook")  # fmt: skip
    assert update.process_update(job, doc_env["deps"]) == "unrunnable"
    body = _last_comment(doc_env)
    assert ("**테스트:** 실행 불가 (의존성을 설치하지 못함: 소스 배포본만 있는 패키지 `docopt`. "
            "wheel만 설치합니다)") in body  # fmt: skip
    assert "**문서 규칙:** ⚠️ 위반 1건 (규칙 1개)" in body and "```yaml" in body


@pytest.mark.parametrize(
    ("base", "answer"),
    [
        ({"orders/a.py": "x = 1\n"}, rule_answer()),
        (BASE_FILES, rule_answer(print_rule(check="none", reason="사람이 판단할 규칙"))),
    ],
    ids=["no-rule-documents", "no-rule-needs-a-checker"],
)
def test_ast_grep_is_not_fetched_without_rules_that_need_a_checker(
    doc_env, tmp_path: Path, base: dict, answer: StructuredResult
) -> None:
    doc_env["monkeypatch"].setattr(doc_rules, "sandbox_for", REAL_SANDBOX_FOR)
    doc_env["monkeypatch"].setattr(
        toolcache, "ensure", lambda *a, **k: pytest.fail("ast-grep을 받으면 안 된다")
    )
    use_model(doc_env, FakeModel(answer))
    state = check(doc_env, tmp_path, base, PR_FILES)
    assert not isinstance(state["doc_rules"], NotRun)


def test_changed_sentence_on_base_reauthors_only_that_rule(doc_env, tmp_path: Path) -> None:
    quotes = [line[2:] for line in THREE_RULES.splitlines()][:2]
    readme = f"# demo\n\n## 코드 규칙\n\n### 출력\n\n- {quotes[0]}\n- {quotes[1]}\n"
    model = use_model(doc_env, FakeModel(rule_answer(*(print_rule(quote=q) for q in quotes))))
    check(doc_env, tmp_path, {**BASE_FILES, "README.md": readme}, PR_FILES)

    edited = quotes[1].replace("`pprint`도", "`pprint`와 `pp`도")
    model.rules = rule_answer(print_rule(quote=quotes[0]), print_rule(quote=edited))
    readme = readme.replace(quotes[1], edited)
    found = check(doc_env, tmp_path, {**BASE_FILES, "README.md": readme}, PR_FILES, pr_number=2)[
        "doc_rules"
    ]
    assert [kind for kind, _ in model.prompts] == [
        "rules",
        "checker",
        "checker",
        "rules",
        "checker",
    ]
    (checker_prompt,) = [p for kind, p in model.prompts[3:] if kind == "checker"]
    assert edited in checker_prompt
    assert [o.checker.cached for o in found.outcomes] == [True, False]
