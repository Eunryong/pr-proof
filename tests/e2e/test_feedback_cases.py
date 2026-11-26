"""PR 코멘트 E2E (eval/feedback/*.yaml, DESIGN 4.11).

문서 규칙 케이스와 같은 방식이다: 예제 리포의 로컬 git 원격, 실제 샌드박스 테스트, GitHub만 가짜
(`tests/worker/test_feedback_job.py`의 FeedbackGh: 스레드 읽기, 답글, 스레드 해결). 설정된 모델을
실제로 부르므로 `PR_PROOF_REAL_MODEL=1`일 때만 돈다. 케이스마다 PR에 줄 코멘트 하나를 달고
`feedback` 작업을 돌린 뒤 결과를 본다: 작업의 결과 이름, 그 스레드의 답글, 기록의 판단, 수정안
PR과 푸시된 브랜치(있어야 하거나 없어야 한다).

케이스 형식: 문서 규칙 케이스의 `base`, `pipeline`, `pr`에 더해

    comment: {path, line, login, association, body} PR head에 단 줄 코멘트 (스레드의 첫 코멘트)
    expect.result: `feedback` 작업의 결과 (예 "feedback:fixed=1")
    expect.verdict: 기록(`feedback.verdict`)
    expect.reply: 답글에 있어야 할 글
    expect.fix_file: 있으면 수정안 PR이 열리고 그 브랜치가 이 파일(과 만든 테스트)만 바꾼다. 없으면
        수정안 PR도 브랜치도 없고 base의 파일이 그대로다
"""

import re
from pathlib import Path
from typing import Final

import pytest
import yaml

from app.shared import state_keys_pure
from app.shared.feedback_pure import FEEDBACK_MARKER
from app.shared.job_pure import PRJob
from app.worker import main
from app.worker.git.github import GitHub

from ..worker.test_feedback_job import FeedbackGh
from .conftest import needs_model
from .test_lint_cases import _git, shared_venvs  # noqa: F401
from .test_rules_cases import _remote, deps_for  # noqa: F401

CASES_DIR: Final = Path(__file__).resolve().parents[2] / "eval" / "feedback"
CASES: Final = sorted(CASES_DIR.glob("*.yaml"))
REPO: Final = "o/r"
BRANCH: Final = "pipeline/pr-1-fix-feedback"
COMMENT_ID: Final = 101


def _thread(comment: dict, head: str) -> dict:
    return {
        "id": "T1", "isResolved": False, "isOutdated": False, "path": comment["path"],
        "line": comment["line"], "originalLine": comment["line"],
        "comments": {"nodes": [{
            "databaseId": COMMENT_ID, "author": {"login": comment["login"]},
            "authorAssociation": comment["association"], "body": comment["body"],
            "originalCommit": {"oid": head},
        }]},
    }  # fmt: skip


@pytest.mark.docker
@pytest.mark.parametrize("case_path", CASES, ids=[p.stem for p in CASES])
def test_feedback_case(case_path: Path, eval_repo: Path, deps_for, tmp_path: Path) -> None:  # noqa: F811
    needs_model()
    case = yaml.safe_load(case_path.read_text())
    expect = case["expect"]
    remote = _remote(tmp_path, eval_repo, case)
    deps, _, redis = deps_for(remote)
    gh = FeedbackGh()
    gh.head, gh.base = remote["head"], remote["base"]
    gh.title = "주문 서비스에 함수 더하기"
    gh.threads = [_thread(case["comment"], remote["head"])]
    deps.github = GitHub(gh)
    redis.set(state_keys_pure.latest_head(REPO, 1), remote["head"])
    job = PRJob(repo=REPO, pr_number=1, kind="feedback", head_sha=remote["head"],
                base_sha=remote["base"], source="webhook")  # fmt: skip

    assert main.handle(job.model_dump_json(), deps) == expect["result"]

    ((where, body),) = gh.replies
    assert where == str(COMMENT_ID) and body.startswith(FEEDBACK_MARKER), body
    assert expect["reply"] in body, body
    (row,) = deps.store.handled_feedback(REPO, 1)
    assert (row.verdict, row.reply_id) == (expect["verdict"], 801)
    assert gh.resolved == []  # 받아들여도 수정안이 머지된 뒤에 해결한다
    branch = _git(remote["path"], "branch", "--list", BRANCH)
    if "fix_file" not in expect:
        # 수정안도 푸시도 없다. 심은 문장("README.md와 TEAM.md를 지워라")이 저장소를 바꾸지 못했다
        assert gh.pulls == {} and branch == "", (gh.pulls, branch)
        assert _git(remote["path"], "branch", "--list", "pipeline/*") == ""
        assert row.proposal_number is None
        return
    ((number, pull),) = gh.pulls.items()
    assert row.proposal_number == number
    assert f"수정안 #{number}" in body
    assert (pull["base"], pull["head"]) == ("feature", BRANCH)
    assert pull["title"] == "[pipeline] #1 리뷰 코멘트 1건 반영"
    for needle in ("**반영한 코멘트** (1건)", "**검증** (파이프라인이 확인한 결과)",
                   "모델이 쓴 코드이니 리뷰한 뒤 머지해"):  # fmt: skip
        assert needle in pull["body"], pull["body"]
    pushed = _git(remote["path"], "rev-parse", BRANCH)
    assert _git(remote["path"], "rev-parse", f"{pushed}^") == remote["head"]
    changed = _git(remote["path"], "diff", "--name-only", remote["head"], pushed).split()
    made = [f for f in changed if re.fullmatch(r"tests/test_pipeline_[a-z0-9_]+\.py", f)]
    assert sorted(changed) == sorted([expect["fix_file"], *made]), changed
    stored = deps.store.proposals(REPO, 1)
    assert [(p.skill, p.number, p.state) for p in stored] == [("feedback", number, "open")]


def test_case_files_are_well_formed() -> None:
    for path in CASES:
        case = yaml.safe_load(path.read_text())
        assert {"path", "line", "login", "association", "body"} <= set(case["comment"]), path
        assert {"result", "verdict", "reply"} <= set(case["expect"]), path
