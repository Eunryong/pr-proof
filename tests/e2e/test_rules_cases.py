"""1단계 평가 케이스 E2E: 문서 규칙 (eval/rules/*.yaml).

lint·static 케이스와 같은 방식이다: 예제 리포의 로컬 git 원격, 실제 샌드박스 테스트, 실제 ast-grep
(도구 캐시에 받는다), GitHub만 가짜. 모델(claude)은 기본으로 케이스의 `model` 답을 돌려주는 가짜라
`pytest -m docker`가 결정적이고 비용이 없다. 원문 확인, 예시 확인, 검사기 실행, 바뀐 줄 거르기는
모두 진짜다. `PR_PROOF_REAL_MODEL=1`이면 로그인된 claude를 실제로 부른다 (비용이 든다. 비교용).

v1 lint·static은 base의 `.pipeline.yaml`로 끈다. 이 케이스는 문서 규칙 절만 본다.
"""

import os
import re
import shutil
from pathlib import Path
from typing import Final

import fakeredis
import pytest
import yaml

from app import config
from app.models import rule_reading, state_keys
from app.models.job import PRJob
from app.worker import main, repo
from app.worker.ai import claude_cli
from app.worker.ai.claude_cli import StructuredResult
from app.worker.deps import Deps
from app.worker.github import GitHub
from app.worker.state import Store
from tests.sandbox_names import TEST_NAMES

from ..worker.test_github import FakeGh
from .test_lint_cases import _bodies, _git, shared_venvs  # noqa: F401

CASES_DIR: Final = Path(__file__).resolve().parents[2] / "eval" / "rules"
CASES: Final = sorted(CASES_DIR.glob("*.yaml"))
REPO: Final = "o/r"
REAL_MODEL_ENV: Final = "PR_PROOF_REAL_MODEL"
# v1 스킬을 끈다. 케이스가 `pipeline`을 주면 그 위에 덮는다
PIPELINE_YAML: Final = {"skills": {"enabled": []}}
_LOCATION: Final = re.compile(r"^- (`+) ?(.+?) ?\1(?: \(파일 이름·경로\))?$")


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


def _answer(data: dict) -> StructuredResult:
    return StructuredResult(ok=True, error=None, data=data, model_id="scripted",
                            input_tokens=0, output_tokens=0, cost_usd=0.0,
                            permission_denials=0)  # fmt: skip


def scripted_model(case: dict):
    """규칙 읽기에는 케이스의 규칙 목록을, 검사기 작성에는 프롬프트에 든 규칙의 검사기를 답한다."""
    rules = case["model"]["rules"]
    reading = {"rules": [
        {"file": "README.md", "section": r["section"], "quote": r["quote"],
         "scope": r.get("scope", []), "scope_exclude": r.get("scope_exclude", []),
         "scope_reason": r.get("scope_reason", ""), "examples": [], "check": "checker",
         "reason": "", "fix": "model"}
        for r in rules
    ]}  # fmt: skip

    def ask(prompt: str, *, schema, **kw) -> StructuredResult:
        if schema is rule_reading.ANSWER_SCHEMA:
            return _answer(reading)
        (rule,) = [r for r in rules if r["quote"] in prompt]
        return _answer({"checker": rule["checker"]["body"],
                        "examples": rule["checker"]["examples"]})  # fmt: skip

    return ask


def doc_rules_section(body: str) -> str:
    start = body.index("**문서 규칙:**")
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

    def make(remote: dict) -> tuple[Deps, FakeGh, fakeredis.FakeStrictRedis]:
        monkeypatch.setattr(repo, "remote_url", lambda name: str(remote["path"]))
        redis = fakeredis.FakeStrictRedis()
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
    redis.set(state_keys.latest_head(REPO, pr), remote["head"])
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
    if not real_model:
        monkeypatch.setattr(claude_cli, "run_structured", scripted_model(case))
    remote = _remote(tmp_path, eval_repo, case)
    deps, gh, redis = deps_for(remote)

    section = doc_rules_section(_check(deps, gh, redis, remote, pr=1))
    found = flagged(section)
    assert found == sorted(expect["violations"]), section
    assert not set(found) & set(expect.get("not_flagged", ())), section
    if expect["violations"]:
        assert "**문서 규칙:** ⚠️ 위반" in section, section
    else:
        assert "**문서 규칙:** ✅ 위반 없음" in section, section
    assert "확인: 검사기 `" in section, "검사기가 예시를 통과해 채택되어야 한다"
    for text in expect.get("contains", ()):
        assert text in section, (text, section)
    for text in expect.get("absent", ()):
        assert text not in section, (text, section)

    if expect.get("second_pr_without_calls"):
        again = doc_rules_section(_check(deps, gh, redis, remote, pr=2))
        assert "모델 호출 0회" in again, again
        assert flagged(again) == found


def test_case_files_are_well_formed() -> None:
    for path in CASES:
        case = yaml.safe_load(path.read_text())
        assert {"name", "description", "pr", "model", "expect"} <= case.keys(), path
        for rule in case["model"]["rules"]:
            assert {"section", "quote", "checker"} <= rule.keys(), (path, rule)
        assert all(re.fullmatch(r"[\w/.-]+:\d+", v) for v in case["expect"]["violations"]), path


def test_flagged_reads_locations_from_section() -> None:
    section = (
        "**문서 규칙:** ⚠️ 위반 2건 (규칙 1개)\n\n위반:\n"
        "- `R-readme-1` `` `x` `` · 2건\n  출처: `README.md > 코드 규칙`\n"
        "  - `a/b.py:3`\n\n<details><summary><code>R-readme-1</code>의 나머지 1건</summary>\n\n"
        "  - ``a`b.py:4``\n\n</details>\n\n모델 호출 1회"
    )
    assert flagged(section) == ["a/b.py:3", "a`b.py:4"]
