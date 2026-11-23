"""1단계 평가 케이스 E2E: 문서 규칙 (eval/rules/*.yaml).

lint 케이스와 같은 방식이다: 예제 리포의 로컬 git 원격, 실제 샌드박스 테스트, 실제 ast-grep
(도구 캐시에 받는다), GitHub만 가짜. 모델(claude)은 기본으로 케이스의 `model` 답을 돌려주는 가짜라
`pytest -m docker`가 결정적이고 비용이 없다. 원문 확인, 예시 확인, 검사기 실행, 바뀐 줄 거르기는
모두 진짜다. `PR_PROOF_REAL_MODEL=1`이면 로그인된 claude를 실제로 부른다 (비용이 든다. 비교용).

예제 리포에는 ruff 설정이 없어 도구 설정의 규칙이 없다. 이 케이스는 문서 규칙과 모델 수정안만
본다. 도구 선택에는 케이스의 규칙에 `tool`이 없으면 "맞는 도구 없음"으로 답한다.
수정안 PR의 생애를 보는 케이스는 `deliver_retry`(첫 push를 실패시키고 재시도),
`recheck_without_calls`(같은 커밋의 `/recheck`), `rejected_next_commit`(수정안 PR을 닫고 새 커밋)을
차례로 한다. `fix_command`는 결과 코멘트 뒤에 그 명령(`/fix` 등)을 달고 수정 결과를 다시 본다.
`real_only: true`인 케이스는 가짜 답(`model`)이 없고 실제 모델로만 돈다(2026-11-22 저장소 주인:
가짜 모델을 없애고 실제 모델로 확인한다). 기대는 결과로 적는다. `expect.generated_tests`는 수정안
PR에 만든 테스트 파일(tests/test_pipeline_*.py)이 같이 올라가야 한다는 뜻이다.
케이스에 `fix`가 있으면 수정 판단은 `model.skip`에 적은 위반만 skip하고 나머지를 fix로 답하며, 수정
작성은 케이스의 `fix` 편집을 모델 폴더에 적용하는 가짜다(검증과 수정안 PR 푸시는 진짜). `fix`가 없는
케이스는 모든 위반을 skip으로 답해 수정까지 가지 않는다. `model.skip`의 값은 이유 글이거나
{why, reason}이다(reason은 수정 판단 답의 skip 이유 이름). `expect.body_contains`는 결과
코멘트 전체에서 찾는다.
"""

import json
import os
import re
import shutil
from pathlib import Path
from typing import Final

import pytest
import yaml

from app.shared import config, state_keys_pure
from app.shared.job_pure import PRJob
from app.worker import main, retry, rule_reading_pure
from app.worker.core.rule_pure import rule_id
from app.worker.deps import Deps
from app.worker.fix import fix_decision_pure
from app.worker.git import repo
from app.worker.git.github import GitHub
from app.worker.model.claude import claude_cli
from app.worker.model.claude.claude_cli import ClaudeResult, StructuredResult
from app.worker.rules import judge_pure, tool_choice_pure
from app.worker.store.state import Store
from tests.sandbox_names import TEST_NAMES

from ..redis_stand_ins import MemoryKeys
from ..worker.git.test_github import FakeGh
from .test_lint_cases import _bodies, _git, shared_venvs  # noqa: F401

CASES_DIR: Final = Path(__file__).resolve().parents[2] / "eval" / "rules"
CASES: Final = sorted(CASES_DIR.glob("*.yaml"))
REPO: Final = "o/r"
REAL_MODEL_ENV: Final = "PR_PROOF_REAL_MODEL"
# base의 .pipeline.yaml. 케이스가 `pipeline`을 주면 그 위에 덮는다
PIPELINE_YAML: Final = {}
NO_TOOL: Final = {"tool": "none", "codes": [], "reason": "케이스", "examples": []}
BRANCH: Final = "pipeline/pr-1-fix-model"
# 위치 뒤에 수정 판단(" · 고칠 대상 (...)", " · 이유")이 붙을 수 있다
_LOCATION: Final = re.compile(r"^- (`+) ?(.+?) ?\1(?: \(파일 이름·경로\))?(?: · .+)?$")


def _apply(root: Path, edits: list[dict] | None) -> None:
    for edit in edits or []:
        path = root / edit["file"]
        if "create" in edit:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(edit["create"])
            continue
        text = path.read_text()
        if "find" in edit:
            assert edit["find"] in text, f"케이스 오류: {edit['file']}에 {edit['find']!r} 없음"
            text = text.replace(edit["find"], edit["replace"], 1)
        text += edit.get("append", "")
        path.write_text(text)


def _remote(tmp_path: Path, eval_repo: Path, case: dict) -> dict:
    """base 커밋(케이스의 base 변경과 .pipeline.yaml)과 PR 커밋(refs/pull/1/head)이 있는 원격."""
    path = tmp_path / "remote"
    shutil.copytree(eval_repo, path, ignore=shutil.ignore_patterns(".git"))
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "uploadpack.allowAnySHA1InWant", "true")
    _apply(path, case.get("base"))
    pipeline = {**PIPELINE_YAML, **(case.get("pipeline") or {})}
    (path / ".pipeline.yaml").write_text(yaml.safe_dump(pipeline))
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "base")
    base = _git(path, "rev-parse", "HEAD")
    _git(path, "checkout", "-q", "-b", "feature")
    _apply(path, case["pr"])
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "pr")
    head = _git(path, "rev-parse", "HEAD")
    for pr in (1, 2):
        _git(path, "update-ref", f"refs/pull/{pr}/head", head)
    _git(path, "checkout", "-q", "main")
    return {"path": path, "base": base, "head": head}


def _push_commit(remote: dict, edits: list[dict]) -> dict:
    """원본 PR(#1)의 브랜치에 새 커밋을 올린다."""
    path = remote["path"]
    _git(path, "checkout", "-q", "feature")
    _apply(path, edits)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "next")
    head = _git(path, "rev-parse", "HEAD")
    _git(path, "update-ref", "refs/pull/1/head", head)
    _git(path, "checkout", "-q", "main")
    return {**remote, "head": head}


def _answer(data: dict) -> StructuredResult:
    return StructuredResult(ok=True, error=None, data=data, model_id="scripted",
                            input_tokens=0, output_tokens=0, cost_usd=0.0,
                            permission_denials=0)  # fmt: skip


_DECIDE_BLOCK: Final = re.compile(
    r"^<<<VIOLATION id=(\S+) .*?^파일: (.+?)\n줄: (\d+)$", re.M | re.S
)


def scripted_model(case: dict):
    """규칙 읽기에는 케이스의 규칙 목록을, 도구 선택에는 그 규칙의 `tool`(없으면 맞는 도구 없음)을,
    검사기 작성에는 프롬프트에 든 규칙의 검사기를 답한다. 모델 판단에는 프롬프트에 든 규칙의
    `judge` 답 중 프롬프트에 든 파일의 것을 답한다(없으면 위반 없음). 검사기의 `coverage`(없으면
    all)와 `uncovered`는 내용 검사기의 답에 넣는다.
    수정 판단에는 케이스에 `fix`가 있으면 `model.skip`의 위반만 skip, 나머지는 fix로 답한다."""
    skip = case["model"].get("skip") or {}
    rules = case["model"]["rules"]
    reading = {"rules": [
        {"file": "README.md", "section": r["section"], "quote": r["quote"],
         "scope": r.get("scope", []), "scope_exclude": r.get("scope_exclude", []),
         "scope_reason": r.get("scope_reason", ""), "examples": [], "check": "checker",
         "reason": "", "fix": "model"}
        for r in rules
    ]}  # fmt: skip

    def ask(prompt: str, *, schema, **kw) -> StructuredResult:
        if schema is rule_reading_pure.ANSWER_SCHEMA:
            return _answer(reading)
        if schema is tool_choice_pure.ANSWER_SCHEMA:
            (rule,) = [r for r in rules if r["quote"] in prompt]
            return _answer(rule.get("tool") or NO_TOOL)
        if schema is judge_pure.ANSWER_SCHEMA:
            files = set(re.findall(r"^파일: (.+)$", prompt, re.M))
            found = [
                {"rule_id": rule_id("README.md", r["quote"]), **answer}
                for r in rules if r["quote"] in prompt
                for answer in r.get("judge", []) if answer["file"] in files
            ]  # fmt: skip
            return _answer({"violations": found})
        if schema is fix_decision_pure.ANSWER_SCHEMA:
            decisions = []
            for vid, file, line in _DECIDE_BLOCK.findall(prompt):
                entry = skip.get(f"{file}:{line}")
                why, reason = (entry.get("why"), entry.get("reason", "")) if isinstance(
                    entry, dict) else (entry, "")  # fmt: skip
                action = "skip" if entry or "fix" not in case else "fix"
                decisions.append({"id": vid, "action": action, "why": why or "케이스",
                                  "reason": reason if action == "skip" else ""})  # fmt: skip
            return _answer({"decisions": decisions})
        own = prompt.split("<<<RECORDS")[0]  # 검색이 넣은 다른 규칙의 기록은 보지 않는다
        (rule,) = [r for r in rules if r["quote"] in own]
        # coverage·uncovered는 내용 검사기의 답에만 있다(경로 패턴의 스키마에는 없다, 4.4)
        extra = ({"coverage": rule["checker"].get("coverage", "all"),
                  "uncovered": rule["checker"].get("uncovered", "")}
                 if "coverage" in schema["properties"] else {})  # fmt: skip
        return _answer({"checker": rule["checker"]["body"],
                        "examples": rule["checker"]["examples"], **extra})  # fmt: skip

    return ask


def scripted_fixer(case: dict):
    """수정 작성의 가짜 claude. 회차마다 `fix.edits`의 다음 편집(모자라면 마지막 것)을 모델 폴더에
    적용하고 `fix.answer`를 마지막 답으로 낸다. 답의 `rule`은 `model.rules`의 순번이다."""
    rules = case["model"]["rules"]
    answer = dict(case["fix"]["answer"])
    answer["changes"] = [
        {**c, "rule": rule_id("README.md", rules[c["rule"]]["quote"])} for c in answer["changes"]
    ]
    calls: list[str] = []

    def run(work: Path, prompt: str, **kw) -> ClaudeResult:
        edits = case["fix"]["edits"][min(len(calls), len(case["fix"]["edits"]) - 1)]
        calls.append(prompt)
        for edit in edits:
            path = work / edit["file"]
            text = path.read_text()
            if edit["find"] in text:
                path.write_text(text.replace(edit["find"], edit["replace"], 1))
        return ClaudeResult(ok=True, error=None, cost_usd=0.0, num_turns=1, permission_denials=(),
                            text=json.dumps(answer, ensure_ascii=False))  # fmt: skip

    run.calls = calls
    return run


def doc_rules_section(body: str) -> str:
    """ "규칙:" 줄부터 끝까지. 규칙이 모두 PR 단위 규칙이고 그 위반이 있으면 "규칙:" 줄이 없고
    "PR 단위:" 줄이 결론이다 (4.9)."""
    start = body.find("**규칙:**")
    if start < 0:
        start = body.index("**PR 단위:**")
    return body[start : body.index("<sub>", start)]


def flagged(section: str) -> list[str]:
    """ "위반:" 목록의 `파일:줄`."""
    out, listing = [], False
    for line in section.splitlines():
        if line == "위반:":
            listing = True
        elif listing and line.startswith("  - ") and not line.startswith("  - 외 "):
            match = _LOCATION.match(line.strip())
            assert match, line
            out.append(match.group(2))
        elif listing and line and not line.startswith(("  ", "- ", "<details>", "</details>")):
            listing = False
    return sorted(out)


@pytest.fixture
def deps_for(tmp_path: Path, shared_venvs: Path, tool_cache: Path, monkeypatch):  # noqa: F811
    monkeypatch.setenv(config.CONFIG_DIR_ENV, str(tmp_path / "config"))
    data = tmp_path / "data"
    data.mkdir()
    (data / "venvs").symlink_to(shared_venvs, target_is_directory=True)
    (data / "tools").symlink_to(tool_cache, target_is_directory=True)

    def make(remote: dict) -> tuple[Deps, FakeGh, MemoryKeys]:
        monkeypatch.setattr(repo, "remote_url", lambda name: str(remote["path"]))
        redis = MemoryKeys()
        gh = FakeGh(head=remote["head"], base=remote["base"])
        settings = config.PipelineConfig(
            repos=[REPO], data_dir=data,
            sandbox=config.SandboxConfig(image=TEST_NAMES.image,
                                         container_prefix=TEST_NAMES.container_prefix),
        )  # fmt: skip
        return Deps(store=Store(data / "pipeline.db", redis), github=GitHub(gh),
                    settings=settings), gh, redis  # fmt: skip

    return make


def _check(deps: Deps, gh: FakeGh, redis, remote: dict, pr: int) -> str:
    gh.head = remote["head"]
    redis.set(state_keys_pure.latest_head(REPO, pr), remote["head"])
    job = PRJob(repo=REPO, pr_number=pr, kind="update", head_sha=remote["head"],
                base_sha=remote["base"], source="webhook")  # fmt: skip
    main.handle(job.model_dump_json(), deps)
    return _bodies(gh)[-1]


@pytest.mark.docker
@pytest.mark.parametrize("case_path", CASES, ids=[p.stem for p in CASES])
def test_rules_case(case_path: Path, eval_repo: Path, deps_for, tmp_path: Path,
                    monkeypatch) -> None:  # fmt: skip
    case = yaml.safe_load(case_path.read_text())
    expect = case["expect"]
    real_model = os.environ.get(REAL_MODEL_ENV) == "1"
    if real_model and case.get("scripted_only"):
        pytest.skip("가짜 모델로만 확인하는 케이스")
    if not real_model and case.get("real_only"):
        pytest.skip(f"실제 모델로만 확인하는 케이스 ({REAL_MODEL_ENV}=1)")
    fixer = scripted_fixer(case) if "fix" in case else None
    if not real_model:
        monkeypatch.setattr(claude_cli, "run_structured", scripted_model(case))
        if fixer is not None:
            monkeypatch.setattr(claude_cli, "run_claude", fixer)
    remote = _remote(tmp_path, eval_repo, case)
    deps, gh, redis = deps_for(remote)

    if expect.get("deliver_retry"):
        section = doc_rules_section(
            _check_with_failed_push(deps, gh, redis, remote, fixer, monkeypatch)
        )
    else:
        body = _check(deps, gh, redis, remote, pr=1)
        section = doc_rules_section(body)
        for text in expect.get("body_contains", ()):
            assert text in body, (text, body)
    found = flagged(section)
    assert found == sorted(expect["violations"]), section
    assert not set(found) & set(expect.get("not_flagged", ())), section
    if expect["violations"]:
        assert "**규칙:** ⚠️ 위반" in section, section
    else:
        assert "**규칙:** ✅ 위반 없음" in section, section
    if not case.get("real_only"):
        assert "확인: 모델이 쓴 검사기 (" in section, "검사기가 예시를 통과해 채택되어야 한다"
    for text in expect.get("contains", ()):
        assert text in section, (text, section)
    for text in expect.get("absent", ()):
        assert text not in section, (text, section)

    if "fix" in expect:
        _check_fix(expect, section, gh, remote, fixer, real_model)
    if "fix_command" in expect:
        _check_fix_command(expect["fix_command"], deps, gh, remote, fixer, real_model)
    if expect.get("deliver_retry"):
        _check_same_patch_pushed(deps, remote)
    if expect.get("recheck_without_calls"):
        _check_recheck(deps, gh, redis, remote, fixer)
    if "rejected_next_commit" in expect:
        _check_rejection(expect["rejected_next_commit"], deps, gh, redis, remote, fixer)

    if expect.get("second_pr_without_calls"):
        again = doc_rules_section(_check(deps, gh, redis, remote, pr=2))
        assert "모델 호출 0회" in again, again
        assert flagged(again) == found


def _made_calls(deps: Deps) -> list[int]:
    """저장된 결과를 쓴 줄을 뺀 실제 모델 호출 기록."""
    return [c.id for c in deps.store.model_calls(REPO, 1) if not c.cache_hit]


def _check_with_failed_push(deps: Deps, gh: FakeGh, redis, remote: dict, fixer, monkeypatch):
    """2-3: 검증을 통과한 뒤 첫 push를 실패시키고, RQ의 재시도처럼 같은 작업을 다시 돌린다. 모델
    호출 기록(`ai_calls`)에 새 줄이 생기지 않는다."""
    real_push, real_attempt = repo.commit_and_push, retry.attempt
    left = [1]

    def failing_once(*args, **kwargs):
        if left:
            left.pop()
            raise repo.GitError("일부러 실패시킨 push (2-3)")
        return real_push(*args, **kwargs)

    monkeypatch.setattr(repo, "commit_and_push", failing_once)
    with pytest.raises(repo.GitError, match="일부러 실패시킨 push"):
        _check(deps, gh, redis, remote, pr=1)
    assert gh.pulls == {} and _git(remote["path"], "branch", "--list", BRANCH) == ""
    rows, written = deps.store.model_calls(REPO, 1), len(fixer.calls)
    monkeypatch.setattr(retry, "attempt", lambda: (2, True))
    body = _check(deps, gh, redis, remote, pr=1)
    monkeypatch.setattr(retry, "attempt", real_attempt)
    assert deps.store.model_calls(REPO, 1) == rows and len(fixer.calls) == written
    return body


def _changed_lines(patch: str) -> list[str]:
    return [line for line in patch.splitlines()
            if line[:1] in "+-" and not line.startswith(("+++", "---"))]  # fmt: skip


def _check_same_patch_pushed(deps: Deps, remote: dict) -> None:
    """올라간 커밋의 변경이 검증을 통과해 저장한 패치(`fix_attempts`)와 같다."""
    (proposal,) = deps.store.proposals(REPO, 1)
    stored = deps.store.fix_attempt(proposal.attempt_id)
    pushed = _git(remote["path"], "-c", "diff.noprefix=false", "diff", "--no-color",
                  "--no-ext-diff", remote["head"], BRANCH)  # fmt: skip
    assert stored.status == "ready" and _changed_lines(pushed) == _changed_lines(stored.patch)


def _check_recheck(deps: Deps, gh: FakeGh, redis, remote: dict, fixer) -> None:
    """2-4: 같은 커밋에 `/recheck`를 달면 모든 단계의 모델 호출이 0이다."""
    calls, written = _made_calls(deps), len(fixer.calls)
    job = PRJob(repo=REPO, pr_number=1, kind="command", command="/recheck", comment_id=70,
                source="webhook")  # fmt: skip
    assert main.handle(job.model_dump_json(), deps).startswith("recheck:")
    section = doc_rules_section(_bodies(gh)[-1])
    assert _made_calls(deps) == calls and len(fixer.calls) == written, section
    assert "모델 호출 0회 / 상한" in section
    assert "수정 판단: 고칠 대상 1건, 직접 고쳐야 함 0건 · 모델 호출 0회" in section
    assert "이 커밋의 모델 수정안 PR #900이 열려 있습니다." in section, section


def _check_rejection(then: dict, deps: Deps, gh: FakeGh, redis, remote: dict, fixer) -> None:
    """2-6: 사람이 수정안 PR을 닫은 뒤 새 커밋을 올리면 거절 기록이 하나 남고, 같은 줄은
    직접 고쳐야 함(이전에 거절됨)이다. 수정 작성을 다시 부르지 않는다."""
    ((number, pull),) = gh.pulls.items()
    pull["state"] = "closed"
    written = len(fixer.calls)
    section = doc_rules_section(_check(deps, gh, redis, _push_commit(remote, then["edits"]), pr=1))
    (record,) = deps.store.records(REPO, 1)
    assert (record.kind, record.origin, record.fix_pr_number) == ("rejected", "fix_pr", number)
    assert len(fixer.calls) == written and list(gh.pulls) == [number], section
    for text in then["contains"]:
        assert text in section, (text, section)


def _check_fix_command(then: dict, deps: Deps, gh: FakeGh, remote: dict, fixer, real_model):
    """2-11: 결과 코멘트 뒤에 사람이 명령을 단다. 그 뒤의 문서 규칙 절과 수정 결과를 본다."""
    job = PRJob(repo=REPO, pr_number=1, kind="command", command=then["command"], comment_id=71,
                source="webhook")  # fmt: skip
    assert main.handle(job.model_dump_json(), deps) == f"fix:{then['fix']}"
    section = doc_rules_section(_bodies(gh)[-1])
    for text in then.get("contains", ()):
        assert text in section, (text, section)
    for text in then.get("absent", ()):
        assert text not in section, (text, section)
    _check_fix(then, section, gh, remote, fixer, real_model)


def _check_fix(expect: dict, section: str, gh: FakeGh, remote: dict, fixer, real_model: bool):
    """모델 수정의 결과: ready면 수정안 PR과 푸시된 브랜치, failed·budget·none이면 수정안 PR이
    없다."""
    branch = _git(remote["path"], "branch", "--list", BRANCH)
    if expect["fix"] != "ready":
        assert gh.pulls == {} and branch == "", section
        if expect["fix"] == "none":
            assert "**모델 수정안:**" not in section, section
            assert real_model or fixer.calls == []
        elif expect["fix"] == "budget":
            assert "**모델 수정안:** 모델 호출 상한에 걸려 처리하지 못함" in section, section
        else:
            assert "**모델 수정안:** 수정안을 만들지 못함" in section, section
        return
    ((number, pull),) = gh.pulls.items()
    assert f"수정안 PR #{number}을 열었습니다." in section, section
    assert (pull["base"], pull["head"]) == ("feature", BRANCH)
    pushed = _git(remote["path"], "rev-parse", BRANCH)
    assert _git(remote["path"], "rev-parse", f"{pushed}^") == remote["head"]
    changed = _git(remote["path"], "diff", "--name-only", remote["head"], pushed).split()
    if expect.get("generated_tests"):
        tests = [f for f in changed if re.fullmatch(r"tests/test_pipeline_[a-z0-9_]+\.py", f)]
        assert tests and sorted(changed) == sorted([expect["fix_file"], *tests]), changed
    else:
        assert changed == [expect["fix_file"]]
    text = _git(remote["path"], "show", f"{pushed}:{expect['fix_file']}")
    if expect.get("same_as_base") and not real_model:
        assert text == _git(remote["path"], "show", f"{remote['base']}:{expect['fix_file']}")
    for needle in expect.get("pushed_contains", ()):
        assert needle in text, (needle, text)
    for needle in expect.get("pr_contains", ()):
        assert needle in pull["body"], (needle, pull["body"])
    for needle in expect.get("pr_absent", ()):
        assert needle not in pull["body"], (needle, pull["body"])
    assert "**검증** (파이프라인이 확인한 결과)" in pull["body"]
    # 위반의 줄을 고친 곳마다 (파일, 규칙)의 설명이 줄 리뷰 코멘트가 된다. 다시 전달해도 리뷰는 하나
    (review,) = gh.reviews[number]
    assert review["comments"] and {c["path"] for c in review["comments"]} == {expect["fix_file"]}


def test_case_files_are_well_formed() -> None:
    for path in CASES:
        case = yaml.safe_load(path.read_text())
        assert {"name", "description", "pr", "expect"} <= case.keys(), path
        assert "model" in case or case.get("real_only"), path
        for rule in case.get("model", {}).get("rules", []):
            assert {"section", "quote", "checker"} <= rule.keys(), (path, rule)
        assert all(re.fullmatch(r"[\w/.-]+:\d+", v) for v in case["expect"]["violations"]), path


def test_flagged_reads_locations_from_section() -> None:
    section = (
        "**규칙:** ⚠️ 위반 2건 (규칙 1개)\n\n위반:\n"
        "- `R-readme-1` `` `x` `` · 2건\n  출처: `README.md > 코드 규칙`\n"
        "  - `a/b.py:3` · 고칠 대상 (모델 수정) · 수정안 #900\n"
        "  직접 고쳐야 함 1건 (고치면 동작이 바뀜 1건)\n\n"
        "<details><summary>직접 고쳐야 함 1건의 위치</summary>\n\n"
        "  - ``a`b.py:4`` · 고치면 동작이 바뀜: `x`\n\n</details>\n\n모델 호출 1회"
    )
    assert flagged(section) == ["a/b.py:3", "a`b.py:4"]
