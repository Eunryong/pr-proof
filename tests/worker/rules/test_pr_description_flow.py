"""PR 설명 확인이 그래프에서 도는지 (docs/DESIGN.md 4.5 끝): 규칙이 없어도 테스트 전에 돌고, 결과가
상태의 `pr_description`과 결과 코멘트의 "PR 설명:" 줄로 나오고, `.pipeline.yaml`로 끌 수 있다.

실제 git 체크아웃(로컬 원격), 실제 SQLite. 테스트 실행과 GitHub만 가짜다. 모델은 부르지 않는 경우만
본다(설명이 빔, 호출 상한). 모델을 부르는 경우는 test_pr_description_real_cli.py가 본다.
"""

from dataclasses import replace
from pathlib import Path

import pytest

from app.shared import state_keys_pure
from app.shared.job_pure import PRJob
from app.worker import base_config, graph, update
from app.worker.ai import claude_cli
from app.worker.repo_config_pure import (
    CallSettings,
    ModelSettings,
    PrDescriptionSettings,
    RepoConfig,
)

from ..helpers import BASE, bodies
from .test_doc_rules_flow import doc_env, make_remote  # noqa: F401


def _no_model(*args, **kwargs):
    pytest.fail("이 경우에는 모델을 부르지 않아야 한다")


def _run(env, tmp_path: Path, config: RepoConfig, *, title: str, body: str = ""):
    """규칙 문서가 없는 base와 config.py를 바꾼 PR 커밋 하나(메시지 "c")."""
    base, head = make_remote(tmp_path / "remote", {"config.py": "TIMEOUT = 30\n"},
                             {"config.py": "TIMEOUT = 5\n"})  # fmt: skip
    gh = env["gh"]
    gh.head, gh.base, gh.title, gh.body = head, base, title, body
    env["redis"].set(state_keys_pure.latest_head("o/r", 1), head)
    mp = env["monkeypatch"]
    mp.setattr(base_config, "load", lambda *a, **k: replace(BASE, repo=config))
    mp.setattr(claude_cli, "run_structured", _no_model)
    job = PRJob(repo="o/r", pr_number=1, kind="update", head_sha=head, base_sha=base,
                source="webhook")  # fmt: skip
    deps = env["deps"]
    return graph.run_flow(job, update.flow_context(deps, deps.store.start_run(job), None))


def test_empty_description_is_shown_without_a_model_call(doc_env, tmp_path: Path) -> None:  # noqa: F811
    state = _run(doc_env, tmp_path, RepoConfig(), title="PR 제목")
    assert state["pr_description"].status == "empty" and "test_run" in state
    assert "**PR 설명:** 설명이 비어 있어 견주지 못했습니다." in bodies(doc_env["gh"])[-1]


def test_call_cap_says_it_could_not_compare(doc_env, tmp_path: Path) -> None:  # noqa: F811
    capped = RepoConfig(model=ModelSettings(calls=CallSettings(pr_base=0)))
    state = _run(doc_env, tmp_path, capped, title="파서에서 빈 줄을 건너뛴다")
    d = state["pr_description"]
    assert (d.status, d.gap, d.files) == ("unchecked", "cap", 1)
    body = bodies(doc_env["gh"])[-1]
    assert "**PR 설명:** 견주지 못했습니다 (모델 호출 상한에 걸림)." in body


def test_pipeline_yaml_turns_it_off(doc_env, tmp_path: Path) -> None:  # noqa: F811
    off = RepoConfig(pr_description=PrDescriptionSettings(enabled=False))
    state = _run(doc_env, tmp_path, off, title="파서에서 빈 줄을 건너뛴다")
    assert "pr_description" not in state and "test_run" in state
    assert "PR 설명" not in bodies(doc_env["gh"])[-1]
    assert not any(".title" in a for args, _ in doc_env["gh"].calls for a in args)
