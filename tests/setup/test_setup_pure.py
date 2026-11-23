import json
from pathlib import Path
from typing import Final

import pytest
import yaml

from app.api import webhook
from app.setup import setup_pure as pure
from app.shared import config

HINTS: Final = json.loads((Path(pure.__file__).with_name("install_hints.json")).read_text())


@pytest.mark.parametrize("repo", ["octocat/hello-world", "a/b", "my-org/repo.name_2"])
def test_valid_repo(repo: str) -> None:
    assert pure.repo_problem(repo) is None


@pytest.mark.parametrize(
    "repo", ["", "hello-world", "a/b/c", "https://github.com/a/b", "-a/b", "a/..", "a /b", "a/"]
)
def test_bad_repo_explains_with_example(repo: str) -> None:
    problem = pure.repo_problem(repo)
    assert problem is not None
    assert "repo는 `계정/저장소이름` 꼴로 적습니다 (예: octocat/hello-world)" in problem
    assert "owner/name" not in problem


# --- config.yaml 고치기 ---

KEEP: Final = """# 운영 설정
redis_url: redis://127.0.0.1:6390/0   # 다른 포트
repos:
  # 처음 것
  - one/a
  - one/b  # 두 번째

sync:
  interval_minutes: 10
"""


def test_add_to_block_list_keeps_comments_and_other_settings() -> None:
    text, added = pure.add_repos(KEEP, ["two/c", "one/a"])
    assert added == ["two/c"]
    assert "# 운영 설정" in text and "# 다른 포트" in text and "# 처음 것" in text
    assert "  - one/b  # 두 번째\n  - two/c\n" in text
    data = yaml.safe_load(text)
    assert data["repos"] == ["one/a", "one/b", "two/c"]
    assert data["sync"] == {"interval_minutes": 10} and data["redis_url"].endswith("6390/0")


def test_add_is_idempotent() -> None:
    once, _ = pure.add_repos(KEEP, ["two/c"])
    twice, added = pure.add_repos(once, ["two/c"])
    assert (twice, added) == (once, [])


def test_add_to_flow_list_keeps_trailing_comment() -> None:
    text, added = pure.add_repos("repos: [one/a]   # 허용 목록\ndata_dir: /x\n", ["two/c"])
    assert added == ["two/c"]
    assert text == "repos: [one/a, two/c]   # 허용 목록\ndata_dir: /x\n"


@pytest.mark.parametrize("start", ["", "# 설정\n", "data_dir: /x", "repos:\n", "repos: []\n"])
def test_add_when_there_is_no_list_yet(start: str) -> None:
    text, added = pure.add_repos(start, ["one/a", "two/b"])
    assert added == ["one/a", "two/b"]
    assert yaml.safe_load(text)["repos"] == ["one/a", "two/b"]
    if start.startswith("# 설정"):
        assert text.startswith("# 설정\n")


def test_add_refuses_what_it_cannot_edit_line_by_line() -> None:
    with pytest.raises(pure.ConfigEditError):
        pure.add_repos("repos: &r\n  - one/a\n", ["two/b"])
    with pytest.raises(pure.ConfigEditError):
        pure.add_repos("repos: one/a\n", ["two/b"])  # 목록이 아님


def test_remove_from_block_list_keeps_the_rest() -> None:
    text, removed = pure.remove_repos(KEEP, ["one/b", "not/there"])
    assert removed == ["one/b"]
    assert "# 운영 설정" in text and "# 처음 것" in text and "interval_minutes: 10" in text
    assert yaml.safe_load(text)["repos"] == ["one/a"]


def test_remove_last_leaves_an_empty_list_the_server_accepts(tmp_path: Path) -> None:
    """repos: 만 남으면 null이라 PipelineConfig가 받지 않는다. 빈 목록이면 서버가 뜨고(웹훅은 모두
    허용 목록 밖, 동기화는 할 것이 없다) 나중에 add로 다시 채운다."""
    text, _ = pure.remove_repos("repos:  # 목록\n  - one/a\nsync:\n  interval_minutes: 5\n",
                                ["one/a"])  # fmt: skip
    assert text.startswith("repos: []  # 목록\n")
    (tmp_path / "config.yaml").write_text(text)
    assert config.load_config(tmp_path).repos == []


def test_remove_from_flow_list() -> None:
    text, removed = pure.remove_repos('repos: [one/a, "two/b"]  # c\n', ["two/b"])
    assert (text, removed) == ("repos: [one/a]  # c\n", ["two/b"])
    text, _ = pure.remove_repos(text, ["one/a"])
    assert text == "repos: []  # c\n"


def test_remove_of_missing_repo_changes_nothing() -> None:
    assert pure.remove_repos(KEEP, ["x/y"]) == (KEEP, [])


# --- 웹훅 ---

URL: Final = "https://smee.io/FAKE"


def _hook(hook_id=1, url=URL, events=pure.HOOK_EVENTS, active=True, content_type="json"):
    return pure.Hook(hook_id, url, tuple(events), active, content_type)


def test_hook_events_are_the_ones_the_server_handles() -> None:
    """서버가 처리하지 않는 이벤트는 '처리하지 않는 이벤트'로 버린다. 등록하는 이벤트 넷은 모두
    서버가 읽는다(동작이 맞지 않아 버려도 이유가 '처리하지 않는 동작'이다)."""
    allowed = frozenset({"o/r"})
    payloads = {
        "pull_request": {"pull_request": {"head": {"ref": "x"}}},
        "issue_comment": {"comment": {}, "issue": {}},
        "pull_request_review": {"pull_request": {"head": {"ref": "x"}}},
        "pull_request_review_comment": {"pull_request": {"head": {"ref": "x"}}},
    }
    assert set(payloads) == set(pure.HOOK_EVENTS)
    for event, body in payloads.items():
        result = webhook.parse_event(
            event, {"repository": {"full_name": "o/r"}, "action": "zzz", **body}, None, allowed
        )
        assert isinstance(result, webhook.Ignored) and "처리하지 않는 이벤트" not in result.reason
    push = webhook.parse_event("push", {"repository": {"full_name": "o/r"}}, None, allowed)
    assert isinstance(push, webhook.Ignored) and "처리하지 않는 이벤트" in push.reason


def test_plan_creates_when_no_hook() -> None:
    plan = pure.plan_hook([_hook(url="https://example.com/other")], URL)
    assert (plan.action, plan.hook_id) == ("create", None)


def test_plan_updates_the_hook_at_the_same_url() -> None:
    plan = pure.plan_hook(
        [_hook(7, events=("pull_request", "issue_comment"), active=False, content_type="form")],
        URL,
    )
    assert (plan.action, plan.hook_id) == ("update", 7)
    joined = " ".join(plan.changes)
    assert "Pull request reviews" in joined and "켠다" in joined and "json" in joined
    # 맞는 웹훅이어도 비밀값은 알 수 없어 늘 맞춘다
    assert pure.plan_hook([_hook(7)], URL).changes == (
        "비밀값을 설정 폴더의 webhook_secret으로 맞춘다",
    )


def test_plan_does_not_duplicate_another_pr_proof_hook() -> None:
    other = "https://smee.io/OTHER"
    plan = pure.plan_hook([_hook(3, url=other)], URL)
    assert (plan.action, plan.others) == ("conflict", (other,))
    plan = pure.plan_hook([_hook(3, url="https://box.example/webhook/github")], URL)
    assert plan.action == "conflict"


def test_hook_payload() -> None:
    created = pure.hook_payload(URL, "s", create=True)
    assert created["name"] == "web" and created["events"] == list(pure.HOOK_EVENTS)
    assert created["config"] == {"url": URL, "content_type": "json", "secret": "s",
                                 "insecure_ssl": "0"}  # fmt: skip
    assert "name" not in pure.hook_payload(URL, "s", create=False)


def test_hooks_at_only_takes_the_same_url() -> None:
    hooks = [_hook(1), _hook(2, url="https://smee.io/OTHER"), _hook(3)]
    assert [h.id for h in pure.hooks_at(hooks, URL)] == [1, 3]


@pytest.mark.parametrize(
    ("hooks", "url", "status"),
    [
        ([], URL, "웹훅 없음"),
        ([_hook()], URL, "웹훅 있음, 이벤트 맞음"),
        ([_hook(events=("pull_request",))], URL, "이벤트 빠짐: Issue comments"),
        ([_hook(active=False)], URL, "꺼져 있음"),
        ([_hook()], None, "저장되어 있지 않음"),
    ],
)
def test_hook_status(hooks, url, status) -> None:
    assert status in pure.hook_status(hooks, url)


def test_manual_text_shows_values_but_not_the_secret() -> None:
    text = pure.manual_hook_text("octocat/hello-world", URL, "/c/webhook_secret")
    assert URL in text and "application/json" in text and "/c/webhook_secret 파일의 값" in text
    assert "Pull request review comments" in text


# --- 안내 글 ---


def test_missing_programs_are_listed_together_with_os_hints() -> None:
    text = pure.missing_programs_text(["docker", "gh", "claude"], HINTS, "darwin")
    assert "Docker Desktop" in text and "brew install gh" in text
    assert "claude를 실행해 /login" in text  # 설치만으로는 안 되고 로그인까지
    assert "sudo apt install git" in pure.missing_programs_text(["git"], HINTS, "linux")
    assert "https://cli.github.com/" in pure.missing_programs_text(["gh"], HINTS, "win32")


def test_check_table() -> None:
    rows = [("GitHub 인증", True, ""), ("Claude 로그인", False, "claude 실행 후 /login")]
    table = pure.check_table(rows)
    assert table.splitlines()[1].endswith("통과")
    assert "Claude 로그인" in table and "실패 → claude 실행 후 /login" in table


def test_token_permissions_name_both_token_kinds() -> None:
    text = pure.TOKEN_PERMISSIONS
    for word in ("Contents", "Pull requests", "Issues", "Webhooks", "repo", "admin:repo_hook"):
        assert word in text
