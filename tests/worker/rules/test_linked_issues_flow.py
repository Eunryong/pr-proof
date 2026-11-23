"""이슈 확인이 그래프에서 도는지 (docs/DESIGN.md 4.5 끝 "연결된 이슈 확인"): 규칙이 없어도 PR 설명
확인 다음, 테스트 전에 돌고, 결과가 상태의 `linked_issues`와 결과 코멘트의 "이슈:" 줄("PR 설명:" 줄
아래)로 나오고, `.pipeline.yaml`로 끌 수 있다.

실제 git 체크아웃(로컬 원격), 실제 SQLite. 테스트 실행과 GitHub만 가짜다. 모델은 부르지 않는 경우만
본다(연결된 이슈가 없음, 호출 상한). 모델을 부르는 경우는 test_linked_issues_real_cli.py가 본다.
"""

from dataclasses import replace
from pathlib import Path
from typing import Final

import pytest

from app.shared import state_keys_pure
from app.shared.job_pure import PRJob
from app.worker import graph, update
from app.worker.config import base_config
from app.worker.config.repo_config_pure import (
    CallSettings,
    LinkedIssuesSettings,
    ModelSettings,
    PrDescriptionSettings,
    RepoConfig,
)
from app.worker.model.claude import claude_cli

from ..git.test_github import linked_node
from ..helpers import BASE, bodies
from .test_doc_rules_flow import doc_env, make_remote  # noqa: F401

NO_DESCRIPTION: Final = PrDescriptionSettings(enabled=False)


def _no_model(*args, **kwargs):
    pytest.fail("이 경우에는 모델을 부르지 않아야 한다")


def _run(env, tmp_path: Path, config: RepoConfig):
    """규칙 문서가 없는 base와 config.py를 바꾼 PR 커밋 하나."""
    base, head = make_remote(tmp_path / "remote", {"config.py": "TIMEOUT = 30\n"},
                             {"config.py": "TIMEOUT = 5\n"})  # fmt: skip
    gh = env["gh"]
    gh.head, gh.base, gh.title, gh.body = head, base, "PR 제목", ""
    env["redis"].set(state_keys_pure.latest_head("o/r", 1), head)
    mp = env["monkeypatch"]
    mp.setattr(base_config, "load", lambda *a, **k: replace(BASE, repo=config))
    mp.setattr(claude_cli, "run_structured", _no_model)
    job = PRJob(repo="o/r", pr_number=1, kind="update", head_sha=head, base_sha=base,
                source="webhook")  # fmt: skip
    deps = env["deps"]
    return graph.run_flow(job, update.flow_context(deps, deps.store.start_run(job), None))


def test_no_linked_issue_is_one_line_under_the_description_line(doc_env, tmp_path: Path) -> None:  # noqa: F811
    state = _run(doc_env, tmp_path, RepoConfig())
    assert state["linked_issues"].status == "none" and "test_run" in state
    lines = bodies(doc_env["gh"])[-1].splitlines()
    at = lines.index("**PR 설명:** 설명이 비어 있어 견주지 못했습니다.")
    assert lines[at + 1] == "**이슈:** 연결된 이슈가 없습니다."
    assert doc_env["gh"].linked_lookups == 1
    # 연결된 이슈가 없으면 진행 글("이슈 확인")을 고치지 않는다
    assert not any("진행 중 (이슈 확인)" in b for b in bodies(doc_env["gh"]))


def test_call_cap_says_it_could_not_compare(doc_env, tmp_path: Path) -> None:  # noqa: F811
    doc_env["gh"].linked = [linked_node(7, "타임아웃을 5초로 줄여 주세요", "30초는 너무 깁니다.")]
    capped = RepoConfig(model=ModelSettings(calls=CallSettings(pr_base=0)),
                        pr_description=NO_DESCRIPTION)  # fmt: skip
    state = _run(doc_env, tmp_path, capped)
    d = state["linked_issues"]
    assert (d.status, d.gap, d.issues, d.files) == ("unchecked", "cap", (7,), 1)
    body = bodies(doc_env["gh"])[-1]
    assert "**이슈:** 견주지 못했습니다 (모델 호출 상한에 걸림)." in body
    assert any("진행 중 (이슈 확인)" in b for b in bodies(doc_env["gh"]))
    assert "PR 설명" not in body  # PR 설명 확인을 꺼도 이슈 확인은 돈다


def test_pipeline_yaml_turns_it_off(doc_env, tmp_path: Path) -> None:  # noqa: F811
    doc_env["gh"].linked = [linked_node(7, "타임아웃을 5초로 줄여 주세요")]
    off = RepoConfig(linked_issues=LinkedIssuesSettings(enabled=False))
    state = _run(doc_env, tmp_path, off)
    assert "linked_issues" not in state and "test_run" in state
    assert "**이슈:**" not in bodies(doc_env["gh"])[-1]
    assert doc_env["gh"].linked_lookups == 0
