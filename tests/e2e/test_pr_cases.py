"""PR 단위 확인 E2E (eval/pr/*.yaml, 13절 4단계의 4-11–4-14).

문서 규칙 케이스와 같은 방식이다: 예제 리포의 로컬 git 원격, 실제 샌드박스(테스트, PR 패턴의 고정
스크립트), GitHub만 가짜(PR의 제목과 본문은 케이스가 정한다). 설정된 모델을 실제로 부르므로
`PR_PROOF_REAL_MODEL=1`일 때만 돈다(규칙 읽기, PR 패턴 작성, PR 단위 판단). `PR_PROOF_RECORD_DIR`을
주면 케이스마다 결과를 JSON으로 남긴다.

케이스 형식: 문서 규칙 케이스의 `base`, `pipeline`에 더해

    pr_commits: [{message, edits}] PR 브랜치의 커밋들 (차례로)
    pr_text: {title, body} GitHub이 돌려주는 PR 제목과 본문
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

import pytest
import yaml
from sqlmodel import Session, select

from app.shared.db_pure import ModelCacheEntry

from .conftest import needs_model
from .test_lint_cases import _bodies, _git, shared_venvs  # noqa: F401
from .test_rules_cases import (
    BRANCH,
    _apply,
    _check,
    deps_for,  # noqa: F401
    doc_rules_section,
    flagged,
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
    """규칙 절 전체. PR 단위 위반은 펼친 부분에, 위반 없는 규칙과 사용량은 접는 블록에 있다
    (4.9)."""
    return doc_rules_section(body)


def code_part(section: str) -> str:
    """규칙 절에서 PR 단위 확인의 부분(결론 아래의 "PR 단위:" 줄, PR 단위 위반, 접는 블록)을 뺀
    것."""
    shown = section.split("\n<details>")[0].split("\n**PR 단위 위반")[0]
    return "\n".join(line for line in shown.splitlines() if not line.startswith("**PR 단위:**"))


def pr_flagged(section: str, commits: list[str]) -> list[str]:
    """PR 단위 위반 목록의 자리. 커밋은 케이스의 순번(commit:N)으로 바꾼다."""
    out, listing = [], False
    by_short = {sha[:7]: n for n, sha in enumerate(commits)}
    for line in section.splitlines():
        if line.startswith("**PR 단위 위반"):
            listing = True
        elif listing and line.startswith("  - "):
            match = _PLACE.match(line)
            assert match, line
            short = match.group(2)
            out.append(f"commit:{by_short[short]}" if short else _PLACE_TOKEN[match.group(1)])
        elif listing and line and not line.startswith(("- ", "  ")):
            listing = False
    return sorted(out)


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


@pytest.mark.docker
@pytest.mark.parametrize("case_path", CASES, ids=[p.stem for p in CASES])
def test_pr_case(case_path: Path, eval_repo: Path, deps_for,  # noqa: F811
                 tmp_path: Path) -> None:  # fmt: skip
    needs_model()
    case = yaml.safe_load(case_path.read_text())
    expect = case["expect"]
    remote = _pr_remote(tmp_path, eval_repo, case)
    deps, gh, redis = deps_for(remote)
    pr_text = case.get("pr_text") or {}
    gh.title, gh.body = pr_text.get("title", "PR"), pr_text.get("body", "")
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


def _code_results(body: str, remote: dict) -> list[str]:
    """코드 쪽의 결과: 위반 목록(수정 판단 포함)과 수정안의 결과 문장. 같은 데이터 폴더의 두 번째
    실행은 저장한 답을 써서 호출 수 줄과 수정안 PR 번호(새 커밋이라 새 PR)가 다르므로 뺀다."""
    section = _strip_shas(code_part(doc_rules_section(body)), remote)
    lines = section.splitlines()
    start = lines.index("위반:")
    end = next((i for i in range(start, len(lines)) if not lines[i]), len(lines))
    fixed = [re.sub(r"#\d+", "#N", line.split(" 수정안 PR")[0]) for line in lines
             if line.startswith("**모델 수정안:**")]  # fmt: skip
    return lines[start:end] + fixed


@pytest.mark.docker
def test_pr_text_isolation(eval_repo: Path, deps_for, tmp_path: Path) -> None:  # noqa: F811
    """4-14: PR의 글이 다른 결과를 바꾸지 않는다. 같은 데이터 폴더에서 중립인 글 다음에 제목·본문·
    커밋 메시지에 지시문을 심은 글로 돈다. PR 단위 확인 밖의 단계는 모두 저장한 답을 써야 하고(새
    호출 0, 저장 키가 같음) 코드 결과가 같아야 한다. 지시문을 받은 PR 단위 판단의 답은 기록만
    한다."""
    needs_model()
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
        assert {"name", "description", "pr_commits", "expect"} <= case.keys(), path
        assert not {"model", "fix"} & case.keys(), path  # 모델의 답을 케이스에 적지 않는다
        assert "pr_flagged" in case["expect"], path
        for token in case["expect"]["pr_flagged"]:
            assert re.fullmatch(r"commit:\d+|pr_body|pr_title|branch|changed_paths", token), path


def test_pr_flagged_reads_places_from_the_section() -> None:
    commits = ["a" * 40, "b" * 40]
    section = (
        "**PR 단위 위반 (알리기만 하고 수정안은 만들지 않음):**\n"
        "- `R-1` `x` · 2건\n  출처: `README.md` · 확인: PR 패턴\n"
        "  - 커밋 `bbbbbbb` `update` · `이유`\n  - PR 본문 (빠진 내용)\n\n"
        "PR 단위 규칙 중 위반 없음:\n"
        "- `R-2` `y` · PR 패턴\n"
    )
    assert pr_flagged(section, commits) == ["commit:1", "pr_body"]
