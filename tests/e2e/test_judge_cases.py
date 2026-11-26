"""모델의 직접 판단 E2E (eval/judge/*.yaml, 13절 4단계의 4-1–4-7).

문서 규칙 케이스와 같은 방식이다: 예제 리포의 로컬 git 원격, 실제 샌드박스 테스트, 실제
ast-grep·ruff(도구 캐시에 받는다), GitHub만 가짜. 설정된 모델을 실제로 부르므로
`PR_PROOF_REAL_MODEL=1`일 때만 돈다. `PR_PROOF_RECORD_DIR`을 주면 케이스마다 결과(코멘트의 규칙 절,
모델 호출 기록, 수정안 PR 본문)를 JSON으로 남긴다.

케이스 형식은 문서 규칙 케이스에 아래를 더한다.

    record_only: 결과를 남기기만 하고 위반 목록은 확인하지 않는다 (4-5)
    expect.judge_calls: 모델 판단의 실제 호출 수 (저장된 답을 쓴 것은 세지 않는다)
    expect.next_commit: {edits, contains} 같은 PR에 새 커밋을 올려 모델 판단의 호출이 늘지 않는다
    expect.then_fix: {command, result, judge_calls, violations, absent} 결과 코멘트 뒤의 명령
    expect.mechanical_branch, fix_file: 기계적 수정안 PR
"""

import json
import os
from pathlib import Path
from typing import Final

import pytest
import yaml

from app.shared.job_pure import PRJob
from app.worker import main
from app.worker.deps import Deps

from .conftest import needs_model
from .test_lint_cases import _bodies, _git, shared_venvs  # noqa: F401
from .test_rules_cases import (
    _check,
    _check_fix,
    _push_commit,
    _remote,
    deps_for,  # noqa: F401
    doc_rules_section,
    flagged,
)

CASES_DIR: Final = Path(__file__).resolve().parents[2] / "eval" / "judge"
CASES: Final = sorted(CASES_DIR.glob("*.yaml"))
REPO: Final = "o/r"
RECORD_ENV: Final = "PR_PROOF_RECORD_DIR"


def _judge_calls(deps: Deps) -> int:
    return sum(1 for c in deps.store.model_calls(REPO, 1) if c.stage == "judge" and not c.cache_hit)


def _record(case_path: Path, deps: Deps, gh, sections: list[str]) -> None:
    folder = os.environ.get(RECORD_ENV)
    if not folder:
        return
    calls = [
        {"stage": c.stage, "model": c.model_id or c.model, "counted": c.counted, "ok": c.ok,
         "cache_hit": c.cache_hit, "input_tokens": c.input_tokens,
         "output_tokens": c.output_tokens, "error": c.error}
        for c in deps.store.model_calls(REPO, 1)
    ]  # fmt: skip
    out = {
        "case": case_path.stem,
        "sections": sections,
        "calls": calls,
        "pulls": [p["body"] for p in gh.pulls.values()],
    }
    Path(folder).mkdir(parents=True, exist_ok=True)
    path = Path(folder) / f"{case_path.stem}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2))


@pytest.mark.docker
@pytest.mark.parametrize("case_path", CASES, ids=[p.stem for p in CASES])
def test_judge_case(case_path: Path, eval_repo: Path, deps_for, tmp_path: Path) -> None:  # noqa: F811
    needs_model()
    case = yaml.safe_load(case_path.read_text())
    expect = case["expect"]
    remote = _remote(tmp_path, eval_repo, case)
    deps, gh, redis = deps_for(remote)

    section = doc_rules_section(_check(deps, gh, redis, remote, pr=1))
    sections = [section]
    try:
        if case.get("record_only"):
            return
        assert flagged(section) == sorted(expect["violations"]), section
        assert not set(flagged(section)) & set(expect.get("not_flagged", ())), section
        for text in expect.get("contains", ()):
            assert text in section, (text, section)
        if "judge_calls" in expect:
            assert _judge_calls(deps) == expect["judge_calls"], section
        if "fix" in expect:
            _check_fix(expect, section, gh, remote, deps)
        if "mechanical_branch" in expect:
            _check_mechanical(expect, section, gh, remote)
        if "next_commit" in expect:
            sections.append(_check_next_commit(expect["next_commit"], deps, gh, redis, remote))
        if "then_fix" in expect:
            sections.append(_check_then_fix(expect["then_fix"], deps, gh))
    finally:
        _record(case_path, deps, gh, sections)


def _check_mechanical(expect: dict, section: str, gh, remote: dict) -> None:
    """4-7: 모델 단계가 멈춰도 도구 검사와 기계적 수정은 계속되어 기계적 수정안 PR이 열린다."""
    ((number, pull),) = gh.pulls.items()
    assert pull["head"] == expect["mechanical_branch"], pull
    assert f"수정안 PR #{number}을 열었습니다." in section, section
    pushed = _git(remote["path"], "rev-parse", expect["mechanical_branch"])
    changed = _git(remote["path"], "diff", "--name-only", remote["head"], pushed).split()
    assert changed == [expect["fix_file"]]


def _check_next_commit(then: dict, deps: Deps, gh, redis, remote: dict) -> str:
    """4-2: 같은 hunk를 가진 다음 커밋은 모델 판단을 부르지 않고 같은 답을 쓴다."""
    before = flagged(doc_rules_section(_bodies(gh)[-1]))
    calls = _judge_calls(deps)
    section = doc_rules_section(_check(deps, gh, redis, _push_commit(remote, then["edits"]), pr=1))
    assert _judge_calls(deps) == calls, section
    assert flagged(section) == before, section
    for text in then.get("contains", ()):
        assert text in section, (text, section)
    return section


def _check_then_fix(then: dict, deps: Deps, gh) -> str:
    """4-6: 상한에 걸려 하지 못한 모델 판단을 `/fix`가 이어 한다."""
    job = PRJob(repo=REPO, pr_number=1, kind="command", command=then["command"], comment_id=71,
                source="webhook")  # fmt: skip
    assert main.handle(job.model_dump_json(), deps) == then["result"]
    section = doc_rules_section(_bodies(gh)[-1])
    assert _judge_calls(deps) == then["judge_calls"], section
    assert flagged(section) == sorted(then["violations"]), section
    for text in then.get("absent", ()):
        assert text not in section, (text, section)
    return section


def test_case_files_are_well_formed() -> None:
    for path in CASES:
        case = yaml.safe_load(path.read_text())
        assert {"name", "description", "pr", "expect"} <= case.keys(), path
        assert not {"model", "fix"} & case.keys(), path  # 모델의 답을 케이스에 적지 않는다
