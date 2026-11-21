"""저장소의 도구 연결 E2E (eval/tools/*.yaml, 13절 3단계의 3-2–3-5).

문서 규칙 케이스와 같은 방식이다: 예제 리포의 로컬 git 원격, 실제 샌드박스 테스트, 실제
ruff·ast-grep(케이스마다 빈 도구 캐시에 받는다), GitHub만 가짜. 모델은 케이스의 `model` 답을
돌려주는 가짜다.
도구 캐시를 케이스마다 비워 두고 무엇을 받았는지(`fetched`, `not_fetched`) 본다.

케이스 형식은 문서 규칙 케이스(eval/README.md)에 아래를 더한다.

    model.rules[].tool: 도구 선택의 답 ({tool, codes, reason, examples}). 없으면 맞는 도구 없음
    expect.rule_line: 코멘트 맨 위의 "규칙:" 줄
    expect.fetched / not_fetched: 도구 캐시의 `<도구>/<버전>` (not_fetched는 `<도구>`도 된다)
    expect.branch, fix_file, same_as_base, pr_contains: 기계적 수정안 PR
    expect.second_run_fetches_nothing: 같은 데이터 폴더로 두 번째 PR을 검사해 받는 것이 없다
"""

from pathlib import Path
from typing import Final

import fakeredis
import pytest
import yaml

from shared import config
from shared import state_keys_pure as state_keys
from shared.job_pure import PRJob
from tests.sandbox_names import TEST_NAMES
from worker import main, repo
from worker.ai import claude_cli
from worker.deps import Deps
from worker.github import GitHub
from worker.sandbox import toolcache
from worker.state import Store

from ..worker.test_github import FakeGh
from .test_lint_cases import _bodies, _git, shared_venvs  # noqa: F401
from .test_rules_cases import _remote, doc_rules_section, scripted_model

CASES_DIR: Final = Path(__file__).resolve().parents[2] / "eval" / "tools"
CASES: Final = sorted(CASES_DIR.glob("*.yaml"))
REPO: Final = "o/r"


def _fetched(tools: Path) -> set[str]:
    """도구 캐시에 완성된 `<도구>/<버전>`."""
    return {
        f"{d.parent.parent.name}/{d.parent.name}"
        for d in tools.glob("*/*/*")
        if (d / toolcache.COMPLETE).is_file()
    }


def _check(deps: Deps, gh: FakeGh, redis, remote: dict, pr: int) -> str:
    gh.head = remote["head"]
    redis.set(state_keys.latest_head(REPO, pr), remote["head"])
    job = PRJob(repo=REPO, pr_number=pr, kind="update", head_sha=remote["head"],
                base_sha=remote["base"], source="webhook")  # fmt: skip
    main.handle(job.model_dump_json(), deps)
    return [b for b in _bodies(gh) if "### pr-proof" in b][-1]


@pytest.mark.docker
@pytest.mark.parametrize("case_path", CASES, ids=[p.stem for p in CASES])
def test_tool_case(case_path: Path, eval_repo: Path, shared_venvs: Path,  # noqa: F811
                   tmp_path: Path, monkeypatch) -> None:  # fmt: skip
    case = yaml.safe_load(case_path.read_text())
    expect = case["expect"]
    monkeypatch.setattr(claude_cli, "run_structured", scripted_model(case))
    downloads: list[str] = []
    real_download = toolcache.docker_download

    def counting(asset, archive, **kwargs):
        downloads.append(asset.url)
        real_download(asset, archive, **kwargs)

    monkeypatch.setattr(toolcache, "docker_download", counting)
    monkeypatch.setenv(config.CONFIG_DIR_ENV, str(tmp_path / "config"))
    remote = _remote(tmp_path, eval_repo, case)
    monkeypatch.setattr(repo, "remote_url", lambda name: str(remote["path"]))
    data = tmp_path / "data"
    data.mkdir()
    (data / "venvs").symlink_to(shared_venvs, target_is_directory=True)
    redis = fakeredis.FakeStrictRedis()
    gh = FakeGh(head=remote["head"], base=remote["base"])
    settings = config.PipelineConfig(
        repos=[REPO], data_dir=data,
        sandbox=config.SandboxConfig(image=TEST_NAMES.image,
                                     container_prefix=TEST_NAMES.container_prefix),
    )  # fmt: skip
    deps = Deps(store=Store(data / "pipeline.db", redis), github=GitHub(gh), settings=settings)

    body = _check(deps, gh, redis, remote, pr=1)
    section = doc_rules_section(body)
    assert section.splitlines()[0] == expect["rule_line"], body
    for text in expect.get("contains", ()):
        assert text in section, (text, section)
    for text in expect.get("absent", ()):
        assert text not in section, (text, section)
    fetched = _fetched(data / "tools")
    assert set(expect.get("fetched", ())) <= fetched, fetched
    for name in expect.get("not_fetched", ()):
        assert not any(f == name or f.startswith(f"{name}/") for f in fetched), fetched

    branch = expect.get("branch")
    if branch:
        ((number, pull),) = gh.pulls.items()
        assert pull["head"] == branch and f"수정안 PR #{number}을 열었습니다." in section
        for text in expect.get("pr_contains", ()):
            assert text in pull["body"], pull["body"]
        pushed = _git(remote["path"], "rev-parse", branch)
        changed = _git(remote["path"], "diff", "--name-only", remote["head"], pushed).split()
        assert changed == [expect["fix_file"]]
        if expect.get("same_as_base"):
            after = _git(remote["path"], "show", f"{pushed}:{expect['fix_file']}")
            assert after == _git(remote["path"], "show", f"{remote['base']}:{expect['fix_file']}")
    else:
        assert gh.pulls == {}

    if expect.get("second_run_fetches_nothing"):
        first = len(downloads)
        assert first >= 1
        _git(remote["path"], "update-ref", "refs/pull/2/head", remote["head"])
        again = doc_rules_section(_check(deps, gh, redis, remote, pr=2))
        assert again.splitlines()[0].startswith(expect["rule_line"].split(" · 수정안")[0])
        assert len(downloads) == first, downloads  # 3-5: 두 번째 실행은 아무것도 받지 않는다
