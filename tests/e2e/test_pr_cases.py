"""PR 단위 확인 E2E (eval/pr/*.yaml, 13절 4단계의 4-11–4-14).

문서 규칙 케이스와 같은 방식이다: 예제 리포의 로컬 git 원격, 실제 샌드박스(테스트, PR 패턴의 고정
스크립트), GitHub만 가짜(PR의 제목과 본문은 케이스가 정한다). 모델은 기본으로 케이스의 답을 돌려주는
가짜라 `pytest -m docker`가 결정적이다. PR 패턴의 예시 확인과 이 PR의 사실에 맞추기, 모델 판단의 답
확인은 모두 진짜다. `PR_PROOF_REAL_MODEL=1`이면 로그인된 claude를 부르고(규칙 읽기, PR 패턴 작성,
PR 단위 판단), `PR_PROOF_RECORD_DIR`을 주면 케이스마다 결과를 JSON으로 남긴다.

케이스 형식: 문서 규칙 케이스의 `base`, `pipeline`, `model.rules`(코드 규칙), `fix`에 더해

    pr_commits: [{message, edits}] PR 브랜치의 커밋들 (차례로)
    pr_text: {title, body} GitHub이 돌려주는 PR 제목과 본문
    model.pr_rules[]: {section, quote, examples, plan, judge} 확인 대상이 `pr`인 규칙. plan은 PR
        패턴 작성의 답(회차마다면 목록), judge는 PR 단위 판단의 답(sha의 "commit:N"은 N번째 커밋)
    expect.pr_flagged: PR 단위 위반의 자리 ("commit:N", "pr_body", "pr_title", "branch")
    expect.code_flagged: 코드 위반의 `파일:줄`
    expect.no_fix: 수정 판단을 부르지 않고 수정안 PR도 브랜치도 없다 (4-13)
    isolation: {title, body, message} 4-14. 같은 PR을 이 글로 한 번 더 돌린다
        (test_pr_text_isolation)
"""

import json
import os
import re
import shutil
from pathlib import Path
from typing import Final

import fakeredis
import pytest
import yaml
from sqlmodel import Session, select

from shared import config
from shared.db_pure import ModelCacheEntry
from tests.sandbox_names import TEST_NAMES
from worker import repo
from worker import rule_reading_pure as rule_reading
from worker.ai import claude_cli
from worker.deps import Deps
from worker.fix import fix_decision_pure as fix_decision
from worker.github import GitHub
from worker.rules import judge_pure as judge
from worker.rules import pr_check_pure as pr_check
from worker.state import Store

from ..worker.test_github import FakeGh
from .test_lint_cases import _bodies, _git, shared_venvs  # noqa: F401
from .test_rules_cases import (
    BRANCH,
    REAL_MODEL_ENV,
    _answer,
    _apply,
    _check,
    deps_for,  # noqa: F401
    doc_rules_section,
    flagged,
    scripted_fixer,
    scripted_model,
)

CASES_DIR: Final = Path(__file__).resolve().parents[2] / "eval" / "pr"
CASES: Final = sorted(CASES_DIR.glob("*.yaml"))
REPO: Final = "o/r"
RECORD_ENV: Final = "PR_PROOF_RECORD_DIR"
PR_STAGES: Final = frozenset({"pr_plan", "pr_judge"})
_PLACE: Final = re.compile(r"^  - (커밋 `([0-9a-f]{7})`|PR 본문|PR 제목|브랜치 이름|바뀐 파일)")
_PLACE_TOKEN: Final = {"PR 본문": "pr_body", "PR 제목": "pr_title", "브랜치 이름": "branch",
                       "바뀐 파일": "changed_paths"}  # fmt: skip


def _pr_remote(tmp_path: Path, eval_repo: Path, case: dict, messages: list[str] | None = None):
    """base 커밋과 PR 커밋들(`pr_commits`)이 있는 원격. messages를 주면 커밋 메시지를 바꾼다."""
    path = tmp_path / "remote"
    shutil.copytree(eval_repo, path, ignore=shutil.ignore_patterns(".git"))
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "uploadpack.allowAnySHA1InWant", "true")
    _apply(path, case.get("base"))
    (path / ".pipeline.yaml").write_text(yaml.safe_dump(case.get("pipeline") or {}))
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "base")
    base = _git(path, "rev-parse", "HEAD")
    _git(path, "checkout", "-q", "-b", "feature")
    commits = []
    for n, commit in enumerate(case["pr_commits"]):
        _apply(path, commit.get("edits"))
        _git(path, "add", "-A")
        message = messages[n] if messages else commit["message"]
        _git(path, "commit", "-q", "--allow-empty", "--cleanup=verbatim", "-m", message)
        commits.append(_git(path, "rev-parse", "HEAD"))
    _git(path, "update-ref", "refs/pull/1/head", commits[-1])
    _git(path, "checkout", "-q", "main")
    return {"path": path, "base": base, "head": commits[-1], "commits": commits}


def pr_section(body: str) -> str:
    start = body.index("**PR 단위 확인")
    end = body.find("\n\n모델 호출 ", start)
    return body[start:] if end < 0 else body[start:end]


def pr_flagged(section: str, commits: list[str]) -> list[str]:
    """ "PR 단위 위반:" 목록의 자리. 커밋은 케이스의 순번(commit:N)으로 바꾼다."""
    out, listing = [], False
    by_short = {sha[:7]: n for n, sha in enumerate(commits)}
    for line in section.splitlines():
        if line == "PR 단위 위반:":
            listing = True
        elif listing and line.startswith("  - "):
            match = _PLACE.match(line)
            assert match, line
            short = match.group(2)
            out.append(f"commit:{by_short[short]}" if short else _PLACE_TOKEN[match.group(1)])
        elif listing and line and not line.startswith(("- ", "  ")):
            listing = False
    return sorted(out)


def pr_model(case: dict, commits: list[str], prompts: list[dict] | None = None):
    """규칙 읽기에는 코드 규칙과 PR 규칙을, PR 패턴 작성과 PR 단위 판단에는 케이스의 답을, 나머지는
    문서 규칙 케이스의 가짜 모델에 맡긴다. prompts를 주면 모든 호출을 남긴다(4-14)."""
    model = case["model"]
    code = scripted_model({**case, "model": {"rules": [], **model}})
    pr_rules = model.get("pr_rules", [])
    reading = code("", schema=rule_reading.ANSWER_SCHEMA).data["rules"] + [
        {"file": "README.md", "section": r["section"], "quote": r["quote"], "target": "pr",
         "scope": [], "scope_exclude": [], "scope_reason": "", "examples": r.get("examples", []),
         "check": "none", "reason": "PR 규칙", "fix": "none"}
        for r in pr_rules
    ]  # fmt: skip
    plans_asked: dict[str, int] = {}

    def ask(prompt: str, *, schema, **kw):
        if prompts is not None:
            prompts.append({"schema": _stage(schema), "prompt": prompt,
                            "system": kw.get("system_prompt", "")})  # fmt: skip
        if schema is rule_reading.ANSWER_SCHEMA:
            return _answer({"rules": reading})
        if schema is pr_check.PLAN_SCHEMA:
            (rule,) = [r for r in pr_rules if r["quote"] in prompt]
            plans = rule["plan"] if isinstance(rule["plan"], list) else [rule["plan"]]
            n = plans_asked[rule["quote"]] = plans_asked.get(rule["quote"], 0) + 1
            return _answer(plans[min(n, len(plans)) - 1])
        if schema is pr_check.JUDGE_SCHEMA:
            (rule,) = [r for r in pr_rules if r["quote"] in prompt]
            answers = [
                {**a, "sha": commits[int(a["sha"].split(":")[1])] if a.get("sha") else ""}
                for a in rule.get("judge", [])
            ]  # fmt: skip
            return _answer({"violations": answers})
        return code(prompt, schema=schema, **kw)

    return ask


def _stage(schema) -> str:
    names = {id(rule_reading.ANSWER_SCHEMA): "rules", id(judge.ANSWER_SCHEMA): "judge",
             id(fix_decision.ANSWER_SCHEMA): "decide", id(pr_check.PLAN_SCHEMA): "pr_plan",
             id(pr_check.JUDGE_SCHEMA): "pr_judge"}  # fmt: skip
    return names.get(id(schema), "checker")


def _record(case_path: Path, deps, body: str, remote: dict, suffix: str = "") -> None:
    folder = os.environ.get(RECORD_ENV)
    if not folder:
        return
    calls = [
        {"stage": c.stage, "model": c.model_id or c.model, "ok": c.ok, "cache_hit": c.cache_hit,
         "counted": c.counted, "input_tokens": c.input_tokens, "output_tokens": c.output_tokens,
         "error": c.error}
        for c in deps.store.model_calls(REPO, 1)
    ]  # fmt: skip
    out = {"case": case_path.stem, "commits": remote["commits"], "body": body, "calls": calls}
    Path(folder).mkdir(parents=True, exist_ok=True)
    (Path(folder) / f"{case_path.stem}{suffix}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2)
    )


@pytest.fixture
def fresh_deps(shared_venvs: Path, tool_cache: Path, monkeypatch):  # noqa: F811
    """데이터 폴더를 부르는 쪽이 정하는 deps_for. 4-14는 두 실행이 저장한 답을 나누지 않게 한다."""

    def make(remote: dict, data: Path):
        monkeypatch.setenv(config.CONFIG_DIR_ENV, str(data.parent / "config"))
        data.mkdir(parents=True)
        (data / "venvs").symlink_to(shared_venvs, target_is_directory=True)
        (data / "tools").symlink_to(tool_cache, target_is_directory=True)
        monkeypatch.setattr(repo, "remote_url", lambda name: str(remote["path"]))
        redis = fakeredis.FakeStrictRedis()
        gh = FakeGh(head=remote["head"], base=remote["base"])
        settings = config.PipelineConfig(
            repos=[REPO], data_dir=data,
            sandbox=config.SandboxConfig(image=TEST_NAMES.image,
                                         container_prefix=TEST_NAMES.container_prefix),
        )  # fmt: skip
        deps = Deps(store=Store(data / "pipeline.db", redis), github=GitHub(gh), settings=settings)
        return deps, gh, redis

    return make


def _setup(case: dict, tmp_path: Path, eval_repo: Path, deps_for, monkeypatch,  # noqa: F811
           *, messages=None, text=None, prompts=None, data: Path | None = None):  # fmt: skip
    remote = _pr_remote(tmp_path, eval_repo, case, messages)
    deps, gh, redis = deps_for(remote) if data is None else deps_for(remote, data)
    pr_text = text or case.get("pr_text") or {}
    gh.title, gh.body = pr_text.get("title", "PR"), pr_text.get("body", "")
    fixer = scripted_fixer(case) if "fix" in case else None
    if os.environ.get(REAL_MODEL_ENV) != "1":
        monkeypatch.setattr(
            claude_cli, "run_structured", pr_model(case, remote["commits"], prompts)
        )
        if fixer is not None:
            if prompts is not None:
                real_fixer = fixer

                def recording(work, prompt, **kw):
                    prompts.append({"schema": "fix", "prompt": prompt,
                                    "system": kw.get("system_prompt", "")})  # fmt: skip
                    return real_fixer(work, prompt, **kw)

                monkeypatch.setattr(claude_cli, "run_claude", recording)
            else:
                monkeypatch.setattr(claude_cli, "run_claude", fixer)
    return remote, deps, gh, redis, fixer


@pytest.mark.docker
@pytest.mark.parametrize("case_path", CASES, ids=[p.stem for p in CASES])
def test_pr_case(case_path: Path, eval_repo: Path, deps_for, tmp_path: Path,  # noqa: F811
                 monkeypatch) -> None:  # fmt: skip
    case = yaml.safe_load(case_path.read_text())
    expect = case["expect"]
    if os.environ.get(REAL_MODEL_ENV) == "1" and case.get("scripted_only"):
        pytest.skip("가짜 모델로만 확인하는 케이스")
    remote, deps, gh, redis, _ = _setup(case, tmp_path, eval_repo, deps_for, monkeypatch)
    body = _check(deps, gh, redis, remote, pr=1)
    try:
        section = pr_section(body)
        assert pr_flagged(section, remote["commits"]) == sorted(expect["pr_flagged"]), section
        for text in expect.get("pr_contains", ()):
            assert text in section, (text, section)
        for text in expect.get("pr_absent", ()):
            assert text not in section, (text, section)
        rules = doc_rules_section(body)
        assert flagged(rules) == sorted(expect.get("code_flagged", [])), rules
        for text in expect.get("contains", ()):
            assert text in body, (text, body)
        if expect.get("no_fix"):
            _check_no_fix(deps, gh, remote)
        if "fix_targets" in expect:
            _check_fix_targets(expect["fix_targets"], deps, gh, remote)
    finally:
        _record(case_path, deps, body, remote)


def _check_no_fix(deps, gh, remote: dict) -> None:
    """4-13: PR 단위 위반만 있는 PR은 수정 판단도, 수정안 PR도, 파이프라인 브랜치도 없다."""
    stages = {c.stage for c in deps.store.model_calls(REPO, 1)}
    assert not stages & {"decide", "fix"}, stages
    assert gh.pulls == {} and deps.store.proposals(REPO, 1) == []
    assert _git(remote["path"], "branch", "--list", "pipeline/*") == ""


def _check_fix_targets(expected: int, deps, gh, remote: dict) -> None:
    """4-13: 코드 위반과 PR 단위 위반이 같이 있으면 수정안 PR은 코드 위반만 고친다. PR 단위 위반은
    수정 판단의 자료에도, 수정안 PR 본문에도 없다."""
    ((number, pull),) = gh.pulls.items()
    assert pull["head"] == BRANCH, pull
    assert f"위반 {expected}건 수정" in pull["title"], pull["title"]
    assert "PR 단위" not in pull["body"] and "커밋 메시지" not in pull["body"], pull["body"]
    (attempt,) = [deps.store.fix_attempt(p.attempt_id) for p in deps.store.proposals(REPO, 1)]
    assert len(json.loads(attempt.targets_json)) == expected


# --- 4-14 (T27): PR의 글이 다른 결과를 바꾸지 않는다 ---

ISOLATION: Final = CASES_DIR / "06-planted-instructions.yaml"


def _cache_keys(deps) -> dict[str, list[str]]:
    with Session(deps.store.engine) as session:
        rows = list(session.exec(select(ModelCacheEntry)))
    out: dict[str, list[str]] = {}
    for row in rows:
        out.setdefault(row.stage, []).append(row.key)
    return {k: sorted(v) for k, v in out.items()}


def _strip_shas(text: str, remote: dict) -> str:
    for sha in [*remote["commits"], remote["head"]]:
        text = text.replace(sha, "<sha>").replace(sha[:7], "<sha>")
    return re.sub(r"\d+\.\d+초", "<초>", text)


def _run_isolation(case, tmp, eval_repo, fresh_deps, monkeypatch, *, planted: bool):
    prompts: list[dict] = []
    planted_text = case["isolation"]
    messages = (
        [c["message"] for c in case["pr_commits"][:-1]] + [planted_text["message"]]
        if planted else None
    )  # fmt: skip
    text = {"title": planted_text["title"], "body": planted_text["body"]} if planted else None
    remote, deps, gh, redis, _ = _setup(case, tmp, eval_repo, fresh_deps, monkeypatch,
                                        messages=messages, text=text, prompts=prompts,
                                        data=tmp / "data")  # fmt: skip
    body = _check(deps, gh, redis, remote, pr=1)
    pushed = _git(remote["path"], "diff", remote["head"], BRANCH) if gh.pulls else ""
    return {
        "prompts": prompts,
        "keys": _cache_keys(deps),
        "body": body,
        "remote": remote,
        "pulls": [
            {k: _strip_shas(str(v), remote) for k, v in p.items()} for p in gh.pulls.values()
        ],
        "pushed": pushed,
        "deps": deps,
    }


@pytest.mark.docker
def test_pr_text_isolation(eval_repo: Path, fresh_deps, tmp_path: Path, monkeypatch) -> None:
    """4-14: 같은 PR을 중립인 글과, 제목·본문·커밋 메시지에 지시문을 심은 글로 두 번 돌린다. PR 단위
    확인 밖의 모든 프롬프트, 모든 저장 키, 모든 결과가 바이트 단위로 같다. 가짜 모델은 따로 된
    데이터 폴더에서 두 번 부른다(저장한 답을 쓰지 않고 실제로 묻는다)."""
    if os.environ.get(REAL_MODEL_ENV) == "1":
        pytest.skip("실제 모델은 test_pr_text_isolation_real_model이 본다")
    case = yaml.safe_load(ISOLATION.read_text())
    neutral = _run_isolation(
        case, tmp_path / "a", eval_repo, fresh_deps, monkeypatch, planted=False
    )
    planted = _run_isolation(case, tmp_path / "b", eval_repo, fresh_deps, monkeypatch, planted=True)

    def others(run: dict) -> list[dict]:
        return [p for p in run["prompts"] if p["schema"] not in PR_STAGES]

    assert [p["schema"] for p in others(neutral)] == [p["schema"] for p in others(planted)]
    assert others(neutral) == others(planted)
    assert {"rules", "checker", "judge", "decide", "fix"} <= {p["schema"] for p in others(neutral)}
    for p in others(planted):
        for needle in case["isolation"]["needles"]:
            assert needle not in p["prompt"] and needle not in p["system"], (p["schema"], needle)
    # PR 패턴 작성도 PR의 글을 보지 않는다. 그 글이 들어가는 곳은 PR 단위 판단뿐이다
    plans = [p for p in planted["prompts"] if p["schema"] == "pr_plan"]
    assert plans == [p for p in neutral["prompts"] if p["schema"] == "pr_plan"]
    judged = [p for p in planted["prompts"] if p["schema"] == "pr_judge"]
    assert any(case["isolation"]["needles"][0] in p["prompt"] for p in judged)
    keys_a, keys_b = neutral["keys"], planted["keys"]
    assert {k: v for k, v in keys_a.items() if k != "pr_judge"} == {
        k: v for k, v in keys_b.items() if k != "pr_judge"
    }
    assert keys_a["pr_judge"] != keys_b["pr_judge"]
    sections = [doc_rules_section(r["body"]) for r in (neutral, planted)]
    code_parts = [_strip_shas(s.split("**PR 단위 확인")[0], r["remote"])
                  for s, r in zip(sections, (neutral, planted), strict=True)]  # fmt: skip
    assert code_parts[0] == code_parts[1]
    assert neutral["pulls"] == planted["pulls"] and neutral["pushed"] == planted["pushed"]
    assert neutral["pulls"], "코드 위반의 수정안 PR이 열려야 한다"


def _code_results(body: str, remote: dict) -> list[str]:
    """코드 쪽의 결과: 위반 목록(수정 판단 포함)과 수정안의 결과 문장. 같은 데이터 폴더의 두 번째
    실행은 저장한 답을 써서 호출 수 줄과 수정안 PR 번호(새 커밋이라 새 PR)가 다르므로 뺀다."""
    section = _strip_shas(doc_rules_section(body).split("**PR 단위 확인")[0], remote)
    lines = section.splitlines()
    start = lines.index("위반:")
    end = next((i for i in range(start, len(lines)) if not lines[i]), len(lines))
    fixed = [re.sub(r"#\d+", "#N", line.split(" 수정안 PR")[0]) for line in lines
             if line.startswith("**모델 수정안:**")]  # fmt: skip
    return lines[start:end] + fixed


@pytest.mark.docker
def test_pr_text_isolation_real_model(eval_repo: Path, deps_for, tmp_path: Path,  # noqa: F811
                                      monkeypatch) -> None:  # fmt: skip
    """4-14를 실제 모델로 한 번 (opt-in). 같은 데이터 폴더에서 중립인 글 다음에 지시문을 심은 글로
    돈다. PR 단위 확인 밖의 단계는 모두 저장한 답을 써야 하고(새 호출 0, 저장 키가 같음) 코드 결과가
    같아야 한다. 지시문을 받은 PR 단위 판단의 답은 기록만 한다."""
    if os.environ.get(REAL_MODEL_ENV) != "1":
        pytest.skip("PR_PROOF_REAL_MODEL=1일 때만")
    case = yaml.safe_load(ISOLATION.read_text())
    remote = _pr_remote(tmp_path / "a", eval_repo, case)
    deps, gh, redis = deps_for(remote)
    gh.title, gh.body = case["pr_text"]["title"], case["pr_text"]["body"]
    first = _check(deps, gh, redis, remote, pr=1)
    before = {c.id for c in deps.store.model_calls(REPO, 1)}
    keys_before = _cache_keys(deps)
    planted_text = case["isolation"]
    messages = [c["message"] for c in case["pr_commits"][:-1]] + [planted_text["message"]]
    shutil.rmtree(remote["path"])
    remote2 = _pr_remote(tmp_path / "a", eval_repo, case, messages)
    gh.head = remote2["head"]
    gh.title, gh.body = planted_text["title"], planted_text["body"]
    second = _check(deps, gh, redis, remote2, pr=1)
    new = [c for c in deps.store.model_calls(REPO, 1) if c.id not in before]
    keys_after = _cache_keys(deps)
    try:
        made = [c.stage for c in new if not c.cache_hit]
        assert set(made) <= PR_STAGES, made
        assert {k: v for k, v in keys_after.items() if k != "pr_judge"} == {
            k: v for k, v in keys_before.items() if k != "pr_judge"
        }
        assert _code_results(first, remote) == _code_results(second, remote2)
    finally:
        _record(ISOLATION, deps, first, remote, "-neutral")
        _record(ISOLATION, deps, second, remote2, "-planted")


def test_case_files_are_well_formed() -> None:
    assert CASES, "eval/pr 케이스가 없다"
    for path in CASES:
        case = yaml.safe_load(path.read_text())
        assert {"name", "description", "pr_commits", "model", "expect"} <= case.keys(), path
        assert "pr_flagged" in case["expect"], path
        for token in case["expect"]["pr_flagged"]:
            assert re.fullmatch(r"commit:\d+|pr_body|pr_title|branch|changed_paths", token), path
        for rule in case["model"].get("pr_rules", []):
            assert {"section", "quote", "plan"} <= rule.keys(), (path, rule)


def test_pr_flagged_reads_places_from_the_section() -> None:
    commits = ["a" * 40, "b" * 40]
    section = (
        "**PR 단위 확인 (알리기만 하고 수정안은 만들지 않음):** 규칙 2개\n\nPR 단위 위반:\n"
        "- `R-1` `x` · 2건\n  출처: `README.md` · 확인: PR 패턴\n"
        "  - 커밋 `bbbbbbb` `update` · `이유`\n  - PR 본문 (빠진 내용)\n\n"
        "PR 단위 규칙 중 위반 없음:\n"
        "- `R-2` `y` · PR 패턴\n"
    )
    assert pr_flagged(section, commits) == ["commit:1", "pr_body"]
