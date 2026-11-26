import json
from pathlib import Path
from typing import Final

import pytest
import yaml

from app.api import webhook
from app.setup import setup_pure as pure
from app.shared import config
from app.shared.embed_models_pure import E5_SMALL, EMBED_MODELS

HINTS: Final = json.loads((Path(pure.__file__).with_name("install_hints.json")).read_text())


@pytest.mark.parametrize("repo", ["octocat/hello-world", "a/b", "my-org/repo.name_2"])
def test_valid_repo(repo: str) -> None:
    assert pure.repo_problem(repo) is None


@pytest.mark.parametrize(
    "repo", ["", "hello-world", "a/b/c", "https://example.com/a/b", "-a/b", "a/..", "a /b", "a/"]
)
def test_bad_repo_explains_with_example(repo: str) -> None:
    problem = pure.repo_problem(repo)
    assert problem is not None
    assert (
        "repo는 `계정/저장소이름` 꼴이나 GitHub 주소로 적습니다 (예: octocat/hello-world)"
        in problem
    )
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


# --- config.yaml의 model.default ---

WITH_CALLS: Final = """\
# 운영 설정
repos: [one/a]
model:  # 호출 상한
  calls:
    max_pr_max: 7  # 낮춤
sync:
  interval_minutes: 10
"""


def _loaded(text: str, tmp_path: Path) -> config.PipelineConfig:
    (tmp_path / "config.yaml").write_text(text)
    return config.load_config(tmp_path)


def test_default_model_goes_into_a_file_without_a_model_section(tmp_path: Path) -> None:
    text = pure.set_default_model(KEEP, "opus")
    assert text == KEEP + "model:\n  default: opus\n"
    assert _loaded(text, tmp_path).model.default == "opus"
    assert pure.set_default_model(text, None) == KEEP  # 지우면 원래 글로 돌아간다


def test_default_model_joins_calls_under_model_and_keeps_comments(tmp_path: Path) -> None:
    text = pure.set_default_model(WITH_CALLS, "opus")
    assert text == WITH_CALLS.replace("# 호출 상한\n", "# 호출 상한\n  default: opus\n")
    loaded = _loaded(text, tmp_path)
    assert (loaded.model.default, loaded.model.calls.max_pr_max) == ("opus", 7)
    assert loaded.sync.interval_minutes == 10
    assert pure.set_default_model(text, None) == WITH_CALLS


def test_existing_default_model_is_replaced_in_place_with_its_comment() -> None:
    text = "repos: []\nmodel:\n    default: haiku  # 싸게\n    calls: {max_pr_max: 7}\n"
    assert pure.set_default_model(text, "opus") == text.replace("haiku", "opus")
    assert pure.config_default_model(text) == "haiku"
    assert pure.set_default_model(text, "haiku") == text  # 바뀔 것이 없으면 그대로


def test_removing_the_only_key_removes_model_so_the_server_still_starts(tmp_path: Path) -> None:
    """`model:`만 남으면 null이라 PipelineConfig가 받지 않는다. 머리 줄의 주석은 남긴다."""
    text = "repos: []\nmodel:  # 모델\n  default: opus\nsync:\n  interval_minutes: 5\n"
    removed = pure.set_default_model(text, None)
    assert removed == "repos: []\n# 모델\nsync:\n  interval_minutes: 5\n"
    assert _loaded(removed, tmp_path).model.default is None


def test_default_model_that_yaml_would_read_as_another_value_is_quoted(tmp_path: Path) -> None:
    text = pure.set_default_model("repos: []\n", "123")
    assert 'default: "123"' in text and _loaded(text, tmp_path).model.default == "123"
    assert "default: sonnet[1m]" in pure.set_default_model("repos: []\n", "sonnet[1m]")


def test_default_model_without_a_value_changes_nothing() -> None:
    assert pure.set_default_model(KEEP, None) == KEEP
    assert pure.config_default_model(KEEP) is None
    assert pure.config_default_model("") is None


def test_default_model_refuses_what_it_cannot_edit_line_by_line() -> None:
    with pytest.raises(pure.ConfigEditError, match="손으로 고친다"):
        pure.set_default_model("repos: []\nmodel: {calls: {max_pr_max: 7}}\n", "opus")
    with pytest.raises(pure.ConfigEditError):
        pure.set_default_model("repos: []\nmodel: opus\n", "haiku")  # 묶음이 아님
    with pytest.raises(pure.ConfigEditError):
        pure.config_default_model("repos: []\nmodel:\n  default: 3\n")  # 글자가 아님
    with pytest.raises(pure.ConfigEditError):
        pure.set_default_model("repos: []\n", "-x")


# --- config.yaml의 rag.allowed ---


def test_rag_allowed_goes_under_rag_and_keeps_the_embed_model_and_comments(tmp_path: Path) -> None:
    text = WITH_CALLS + "rag:  # 검색\n  embed_model: intfloat/multilingual-e5-small\n"
    assert pure.config_rag_allowed(text) is None
    on = pure.set_rag_allowed(text, True)
    assert on == text.replace("# 검색\n", "# 검색\n  allowed: true\n")
    loaded = _loaded(on, tmp_path)
    assert (loaded.rag.allowed, loaded.model.calls.max_pr_max) == (True, 7)
    off = pure.set_rag_allowed(on, False)
    assert off == on.replace("allowed: true", "allowed: false")
    assert pure.config_rag_allowed(off) is False and _loaded(off, tmp_path).rag.allowed is False
    assert pure.set_rag_allowed(off, False) == off  # 바뀔 것이 없으면 그대로


def test_rag_allowed_goes_into_a_file_without_a_rag_section(tmp_path: Path) -> None:
    text = pure.set_rag_allowed(KEEP, False)
    assert text == KEEP + "rag:\n  allowed: false\n"
    assert _loaded(text, tmp_path).rag.allowed is False


def test_rag_allowed_that_is_not_true_or_false_is_refused() -> None:
    with pytest.raises(pure.ConfigEditError, match="rag.allowed"):
        pure.config_rag_allowed("repos: []\nrag:\n  allowed: 1\n")
    with pytest.raises(pure.ConfigEditError, match="손으로 고친다"):
        pure.set_rag_allowed("repos: []\nrag: {allowed: true}\n", False)


def test_rag_view_shows_the_choice_the_extras_and_the_model() -> None:
    spec = EMBED_MODELS[E5_SMALL]
    unset = pure.rag_view(None, False, False, spec, "/d/models/x", "pr-proof")
    assert "과거 수정 기록 검색 (이 컴퓨터, config.yaml의 rag.allowed): 정하지 않음" in unset
    assert "선택 의존성 pr-proof[rag]: 없음" in unset
    assert f"임베딩 모델 {E5_SMALL}: 받지 않음 (약 487MB)" in unset
    assert "검색 없이 검사합니다" in unset
    assert "켜기: pr-proof rag on   끄기: pr-proof rag off" in unset
    on = pure.rag_view(True, True, True, spec, "/d/models/x", "npx pr-proof")
    assert "rag.allowed): 켬" in on and ": 깔림" in on and "받음 (/d/models/x)" in on
    assert "rag.enabled(기본 auto)" in on and "npx pr-proof rag on" in on
    off = pure.rag_view(False, True, True, spec, "/d/models/x", "pr-proof")
    assert "rag.allowed): 끔" in off and "어느 repo도 검색하지 않습니다" in off


def test_rag_size_says_both_the_packages_and_the_model() -> None:
    text = pure.rag_size_text(EMBED_MODELS[E5_SMALL])
    assert "패키지 수백 MB" in text and "약 487MB" in text


@pytest.mark.parametrize(
    "name",
    [
        "sonnet",
        "opus",
        "claude-sonnet-4-5",
        "sonnet[1m]",
        "inkling-2-mini",
        "hf.co/unsloth/Inkling-2-Mini-GGUF:Q4_K_M",
    ],
)
def test_model_names_of_the_right_shape(name: str) -> None:
    assert pure.model_name_problem(name) is None


@pytest.mark.parametrize("name", ["-x", "", "opus 4", "a;b", "--model"])
def test_bad_model_name_is_explained_with_examples(name: str) -> None:
    problem = pure.model_name_problem(name)
    assert problem is not None and pure.MODEL_EXAMPLES in problem


def test_model_view_without_a_pipeline_value_shows_the_code_defaults() -> None:
    text = pure.model_view(None, "pr-proof")
    assert "파이프라인 쪽 모델: 정하지 않음 (단계별 기본값: 모든 단계 sonnet)" in text
    assert [line.split()[-1] for line in text.splitlines()[3:8]] == ["sonnet"] * 5
    assert "수정 작성 (올린 뒤)      sonnet (수정 작성과 같은 모델이라 올리지 않는다)" in text
    assert ".pipeline.yaml에 단계별로 적은 모델(model.rules 등)은 이 값보다 앞섭니다" in text
    assert "pr-proof model <이름>" in text and "pr-proof model default" in text


def test_model_view_with_a_pipeline_value_uses_it_everywhere() -> None:
    text = pure.model_view("opus", "npx .")
    assert "파이프라인 쪽 모델: opus" in text
    assert "opus (수정 작성과 같은 모델이라 올리지 않는다)" in text
    assert "npx . model <이름>" in text


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


@pytest.mark.parametrize(
    "text",
    [
        "https://github.com/octocat/hello-world",
        "https://github.com/octocat/hello-world.git",
        "https://github.com/octocat/hello-world/pull/3",
        "http://www.github.com/octocat/hello-world/",
        "github.com/octocat/hello-world",
        "git@github.com:octocat/hello-world.git",
        "  octocat/hello-world  ",
    ],
)
def test_pasted_github_address_becomes_repo_name(text: str) -> None:
    """브라우저나 git에서 복사한 주소를 붙여 넣어도 받는다."""
    assert pure.repo_problem(text) is None
    assert pure.repo_name(text) == "octocat/hello-world"


def test_repo_names_differing_only_in_case_are_the_same_repo() -> None:
    text = "repos:\n  - Octocat/Hello-World\n"
    assert pure.add_repos(text, ["octocat/hello-world"]) == (text, [])
    out, gone = pure.remove_repos(text, ["OCTOCAT/hello-world"])
    assert gone == ["Octocat/Hello-World"] and yaml.safe_load(out)["repos"] == []
    assert pure.spelled_as("octocat/HELLO-world", ["a/b", "Octocat/Hello-World"]) == (
        "Octocat/Hello-World"
    )
    assert pure.spelled_as("octocat/other", ["Octocat/Hello-World"]) is None
