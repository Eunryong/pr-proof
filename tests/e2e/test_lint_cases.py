"""도구 설정의 규칙과 기계적 수정 묶음 E2E (eval/lint/*.yaml, 13절 3단계의 3-1).

v1 lint 단계의 케이스 다섯 개를 새 구조로 돌린다. 예제 리포에 케이스의 위반을 넣은 PR을 로컬 git
원격으로 만들고, 워커 작업을 처음부터 끝까지 돌린다: 체크아웃 → 도구 설정의 규칙 묶음(ruff) →
위반 찾기 → 샌드박스 테스트 → 수정 판단 → 기계적 수정 → 검증 → 수정안 PR 푸시.
GitHub만 가짜(FakeGh)이고 git·ruff(도구 캐시에 받음)·Docker는 진짜다. 수정안 브랜치는 로컬 원격에
실제로 푸시된다.

예제 리포에는 ruff 설정이 없다. v1은 그런 base에 ruff 기본 규칙을 썼지만 파이프라인이 정한 규칙은
없앴다(3-3). 그래서 이 케이스의 base에는 빈 `ruff.toml`(ruff의 기본 설정)을 둔다. 케이스 파일은 v1
그대로다. 바뀐 것은 수정안 브랜치의 이름(`fix-lint` → `fix-mechanical`)뿐이다.
이 하네스는 문서 규칙을 보지 않는다(원격에서 규칙 문서 README.md를 지운다). 모델은 PR 설명 확인이
케이스마다 한 번 부르므로 `PR_PROOF_REAL_MODEL=1`일 때만 돈다.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Final

import pytest
import yaml

from app.shared import config, state_keys_pure
from app.shared.job_pure import PRJob
from app.worker import main
from app.worker.config.repo_config_pure import DEFAULT_RULE_DOCS
from app.worker.deps import Deps
from app.worker.git import repo
from app.worker.git.github import GitHub
from app.worker.store.state import Store
from tests.sandbox_names import TEST_NAMES

from ..redis_stand_ins import MemoryKeys
from ..worker.git.test_github import FakeGh
from .conftest import needs_model

CASES_DIR: Final = Path(__file__).resolve().parents[2] / "eval" / "lint"
CASES: Final = sorted(CASES_DIR.glob("*.yaml"))
REPO: Final = "o/r"
BRANCH: Final = "pipeline/pr-1-fix-mechanical"
# 저장소가 ruff를 쓴다는 설정. 내용이 비면 ruff의 기본 설정이다
RUFF_SETTINGS: Final = "ruff.toml"
# 규칙별 위반 목록의 머리 줄: "- `F401` `ruff.toml의 설정대로 ruff를 통과한다` · 2건"
_GROUP: Final = re.compile(r"^- `([^`]+)` .* · (\d+)건$")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip


def _apply(root: Path, edits: list[dict]) -> None:
    for edit in edits or []:
        path = root / edit["file"]
        text = path.read_text()
        if "find" in edit:
            assert edit["find"] in text, f"케이스 오류: {edit['file']}에 {edit['find']!r} 없음"
            text = text.replace(edit["find"], edit["replace"], 1)
        if "append" in edit:
            text += edit["append"]
        if "prepend" in edit:
            text = edit["prepend"] + text
        path.write_text(text)


def _remote(tmp_path: Path, eval_repo: Path, case: dict) -> dict:
    """base 커밋(케이스의 base 변경 포함)과 PR 커밋(refs/pull/1/head)이 있는 로컬 원격."""
    path = tmp_path / "remote"
    shutil.copytree(eval_repo, path, ignore=shutil.ignore_patterns(".git"))
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "uploadpack.allowAnySHA1InWant", "true")
    _apply(path, case.get("base"))
    for doc in DEFAULT_RULE_DOCS:
        (path / doc).unlink(missing_ok=True)
    (path / RUFF_SETTINGS).write_text("")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "base")
    base = _git(path, "rev-parse", "HEAD")
    _git(path, "checkout", "-q", "-b", "feature")
    _apply(path, case["pr"])
    _git(path, "commit", "-q", "-am", "pr")
    head = _git(path, "rev-parse", "HEAD")
    _git(path, "update-ref", "refs/pull/1/head", head)
    _git(path, "checkout", "-q", "main")
    return {"path": path, "base": base, "head": head}


@pytest.fixture(scope="session")
def shared_venvs(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("e2e-venvs")  # 케이스마다 의존성을 다시 설치하지 않는다


def _bodies(gh: FakeGh) -> list[str]:
    """원본 PR(#1) 결과 코멘트에 쓴 본문, 순서대로."""
    out = []
    for args, stdin in gh.calls:
        joined = " ".join(args)
        if stdin and ("issues/1/comments" in joined or "issues/comments/" in joined):
            out.append(json.loads(stdin)["body"])
    return out


def _finding_codes(body: str) -> list[str]:
    """코멘트의 규칙별 위반 목록에서 규칙 id(도구 설정의 규칙은 ruff 코드)를 건수만큼."""
    return sorted(
        code
        for line in body.splitlines()
        if (m := _GROUP.match(line))
        for code in [m.group(1)] * int(m.group(2))
    )


@pytest.mark.docker
@pytest.mark.parametrize("case_path", CASES, ids=[p.stem for p in CASES])
def test_lint_case(case_path: Path, eval_repo: Path, shared_venvs: Path, tmp_path: Path,
                   monkeypatch) -> None:  # fmt: skip
    needs_model()
    case = yaml.safe_load(case_path.read_text())
    expect = case["expect"]
    remote = _remote(tmp_path, eval_repo, case)
    monkeypatch.setattr(repo, "remote_url", lambda name: str(remote["path"]))
    monkeypatch.setenv(config.CONFIG_DIR_ENV, str(tmp_path / "config"))
    data = tmp_path / "data"
    data.mkdir()
    (data / "venvs").symlink_to(shared_venvs, target_is_directory=True)
    redis = MemoryKeys()
    gh = FakeGh(head=remote["head"], base=remote["base"])
    deps = Deps(
        store=Store(data / "pipeline.db", redis),
        github=GitHub(gh),
        settings=config.PipelineConfig(
            repos=[REPO],
            data_dir=data,
            sandbox=config.SandboxConfig(
                image=TEST_NAMES.image, container_prefix=TEST_NAMES.container_prefix
            ),
        ),
    )
    update = PRJob(
        repo=REPO, pr_number=1, kind="update", head_sha=remote["head"], base_sha=remote["base"],
        source="webhook",
    )  # fmt: skip
    redis.set(state_keys_pure.latest_head(REPO, 1), remote["head"])

    status = main.handle(update.model_dump_json(), deps)
    body = [b for b in _bodies(gh) if "### pr-proof" in b][-1]
    rules = next(line for line in body.splitlines() if line.startswith("**규칙:**"))
    assert _finding_codes(body) == sorted(expect["findings"]), body

    match expect["proposal"]:
        case "auto":  # 기본값(require_approval: false): /fix 없이 검사 뒤 바로 수정안까지 간다
            assert status == "passed"
            assert "자동 수정 가능" not in rules, rules
        case "no_fixable":
            assert "자동 수정" not in rules
        case "tests":
            assert status == "failed" and "자동 수정" not in rules
            assert ("직접 고쳐야 함 1건 · 테스트가 실패해 고친 것을 확인할 수 없으므로 "
                    "수정안을 만들지 않았습니다.") in body  # fmt: skip
            assert "**기계적 수정안:**" not in body
    if expect["proposal"] != "auto":
        assert _git(remote["path"], "branch", "--list", BRANCH) == ""
        return

    fixed_body = body
    assert expect["fix"] == "ready" and "**기계적 수정안:**" in fixed_body, fixed_body
    ((number, pull),) = gh.pulls.items()
    assert f"수정안 PR #{number}을 열었습니다." in fixed_body, fixed_body
    assert (pull["base"], pull["head"]) == ("feature", BRANCH)

    # 로컬 원격에 실제로 푸시된 제안 브랜치를 확인한다
    pushed = _git(remote["path"], "rev-parse", BRANCH)
    assert _git(remote["path"], "rev-parse", f"{pushed}^") == remote["head"]
    changed = _git(remote["path"], "diff", "--name-only", remote["head"], pushed).split()
    assert changed == [expect["file"]]
    text = _git(remote["path"], "show", f"{pushed}:{expect['file']}") + "\n"
    for needle in expect.get("contains", []):
        assert needle in text, text
    for needle in expect.get("not_contains", []):
        assert needle not in text, text
    if expect.get("same_as_base"):
        assert text == _git(remote["path"], "show", f"{remote['base']}:{expect['file']}") + "\n"
    assert ("자동으로 고치지 못한 위반" in fixed_body) == bool(expect["remaining"]), fixed_body
