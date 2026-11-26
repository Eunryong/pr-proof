"""1단계 평가 케이스 E2E: 문서 규칙 (eval/rules/*.yaml).

lint 케이스와 같은 방식이다: 예제 리포의 로컬 git 원격, 실제 샌드박스 테스트, 실제 ast-grep
(도구 캐시에 받는다), GitHub만 가짜. 모델은 설정된 모델을 실제로 부르므로 `PR_PROOF_REAL_MODEL=1`일
때만 돈다(비용이 든다). 기대는 글이 아니라 결과로 적는다: 위반의 위치, 수정안이 올라갔는가, 바뀐
파일.

`expect.body_contains`는 결과 코멘트 전체에서, `contains`·`absent`는 규칙 절에서 찾는다.
`expect.generated_tests`는 수정안 PR에 만든 테스트 파일(tests/test_pipeline_*.py)이 같이 올라가야
한다는 뜻이다. `fix_command`는 결과 코멘트 뒤에 그 명령(`/fix` 등)을 달고 수정 결과를 다시 본다.
`second_pr_without_calls`는 같은 커밋의 두 번째 PR이 저장한 답만 쓰는지 본다.
"""

import re
import shutil
from pathlib import Path
from typing import Final

import pytest
import yaml

from app.shared import config, state_keys_pure
from app.shared.job_pure import PRJob
from app.worker import main
from app.worker.deps import Deps
from app.worker.git import repo
from app.worker.git.github import GitHub
from app.worker.store.state import Store
from tests.sandbox_names import TEST_NAMES

from ..redis_stand_ins import MemoryKeys
from ..worker.git.test_github import FakeGh
from .conftest import needs_model
from .test_lint_cases import _bodies, _git, shared_venvs  # noqa: F401

CASES_DIR: Final = Path(__file__).resolve().parents[2] / "eval" / "rules"
CASES: Final = sorted(CASES_DIR.glob("*.yaml"))
REPO: Final = "o/r"
# base의 .pipeline.yaml. 케이스가 `pipeline`을 주면 그 위에 덮는다
PIPELINE_YAML: Final = {}
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
def test_rules_case(case_path: Path, eval_repo: Path, deps_for, tmp_path: Path) -> None:
    needs_model()
    case = yaml.safe_load(case_path.read_text())
    expect = case["expect"]
    remote = _remote(tmp_path, eval_repo, case)
    deps, gh, redis = deps_for(remote)

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
    for text in expect.get("contains", ()):
        assert text in section, (text, section)
    for text in expect.get("absent", ()):
        assert text not in section, (text, section)

    if "fix" in expect:
        _check_fix(expect, section, gh, remote, deps)
    if "fix_command" in expect:
        _check_fix_command(expect["fix_command"], deps, gh, remote)

    if expect.get("second_pr_without_calls"):
        again = doc_rules_section(_check(deps, gh, redis, remote, pr=2))
        assert "모델 호출 0회" in again, again
        assert flagged(again) == found


def _check_fix_command(then: dict, deps: Deps, gh: FakeGh, remote: dict) -> None:
    """2-11: 결과 코멘트 뒤에 사람이 명령을 단다. 그 뒤의 문서 규칙 절과 수정 결과를 본다."""
    job = PRJob(repo=REPO, pr_number=1, kind="command", command=then["command"], comment_id=71,
                source="webhook")  # fmt: skip
    assert main.handle(job.model_dump_json(), deps) == f"fix:{then['fix']}"
    section = doc_rules_section(_bodies(gh)[-1])
    for text in then.get("contains", ()):
        assert text in section, (text, section)
    for text in then.get("absent", ()):
        assert text not in section, (text, section)
    _check_fix(then, section, gh, remote, deps)


def _check_fix(expect: dict, section: str, gh: FakeGh, remote: dict, deps: Deps) -> None:
    """모델 수정의 결과: ready면 수정안 PR과 푸시된 브랜치, failed·budget·none이면 수정안 PR이
    없다."""
    branch = _git(remote["path"], "branch", "--list", BRANCH)
    if expect["fix"] != "ready":
        assert gh.pulls == {} and branch == "", section
        if expect["fix"] == "none":
            assert "**모델 수정안:**" not in section, section
            assert not [c for c in deps.store.model_calls(REPO, 1) if c.stage == "fix"]
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
    # fix_also: 위반을 고치며 같이 바꾼, 위반이 없는 파일
    code = sorted([expect["fix_file"], *expect.get("fix_also", ())])
    if expect.get("generated_tests"):
        tests = [f for f in changed if re.fullmatch(r"tests/test_pipeline_[a-z0-9_]+\.py", f)]
        assert tests and sorted(changed) == sorted([*code, *tests]), changed
    else:
        assert sorted(changed) == code, changed
    text = _git(remote["path"], "show", f"{pushed}:{expect['fix_file']}")
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
        assert not {"model", "fix"} & case.keys(), path  # 모델의 답을 케이스에 적지 않는다
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
