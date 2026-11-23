"""configure·add·rm·list의 흐름. GitHub(gh), Docker, Redis, smee.io, claude는 모두 바꿔 끼운다.

GitHub은 `FakeGitHub`가 받은 gh 인자로 답하고 부른 것을 적는다(기존 테스트가 GhRunner를 바꿔 끼우는
방식). 실제 저장소에는 아무것도 쓰지 않는다.
"""

import json
import stat
from pathlib import Path
from typing import Final

import pytest
import yaml

from app.setup import configure as flow
from app.setup import files
from app.setup import github_setup as gh
from app.setup import setup_pure as pure
from app.shared import config, github_auth
from app.worker import preflight

REPO: Final = "octocat/hello-world"
OTHER: Final = "octocat/spoon-knife"
SMEE: Final = "https://smee.io/FAKEnewchannel"
TOKEN: Final = "github_pat_FAKE2222222222222222222222"
HINTS: Final = json.loads((Path(flow.__file__).with_name("install_hints.json")).read_text())


class FakeGitHub:
    def __init__(self) -> None:
        self.logged_in = True
        self.good_tokens = {TOKEN}
        self.perms: dict[str, tuple[bool, bool]] = {REPO: (True, True), OTHER: (True, True)}
        self.hooks: dict[str, list[dict[str, object]]] = {REPO: [], OTHER: []}
        self.hook_admin = True
        self.writes: list[list[str]] = []
        self.payloads: list[dict[str, object]] = []
        self.tokens_seen: list[str | None] = []
        self.next_id = 100

    def runner(self, token: str | None) -> gh.Runner:
        self.tokens_seen.append(token)
        return lambda args, stdin=None: self.call(token, args, stdin)

    def call(self, token: str | None, args: list[str], stdin: str | None) -> str:
        if args[:2] == ["auth", "status"]:
            ok = token in self.good_tokens if token is not None else self.logged_in
            if not ok:
                raise gh.SetupGitHubError("gh auth status 실패")
            return ""
        if args[:2] == ["api", "user"]:
            return "me\n"
        if "-X" in args:
            if not self.hook_admin:
                raise gh.NoPermission("gh api -X 실패: HTTP 403")
            self.writes.append(args)
            method, path = args[args.index("-X") + 1], args[args.index("-X") + 2]
            repo = "/".join(path.split("/")[1:3])
            if method == "POST":
                payload = json.loads(stdin or "{}")
                self.payloads.append(payload)
                self.next_id += 1
                self.hooks[repo].append({"id": self.next_id, "url": payload["config"]["url"],
                                         "events": payload["events"], "active": True,
                                         "content_type": "json"})  # fmt: skip
                return f"{self.next_id}\n"
            if method == "PATCH":
                self.payloads.append(json.loads(stdin or "{}"))
                return "1\n"
            if method == "DELETE":
                hook_id = int(path.rsplit("/", 1)[1])
                self.hooks[repo] = [h for h in self.hooks[repo] if h["id"] != hook_id]
                return ""
        path = next(a for a in args if a.startswith("repos/"))
        repo = "/".join(path.split("/")[1:3])
        if path.endswith("/hooks"):
            if not self.hook_admin:
                raise gh.NoPermission("gh api --paginate 실패: HTTP 404")
            return "".join(json.dumps(h) + "\n" for h in self.hooks.get(repo, []))
        # GitHub은 이름의 대소문자를 가리지 않고 찾아, 적혀 있는 정식 이름을 돌려준다
        name = next((r for r in self.perms if r.casefold() == repo.casefold()), None)
        if name is None:
            raise gh.NoPermission("gh api repos 실패: HTTP 404")
        read, write = self.perms[name]
        return f"{str(read).lower()}\t{str(write).lower()}\t{name}\n"

    def hook(self, repo: str, url: str, hook_id: int = 1, events=pure.HOOK_EVENTS) -> None:
        self.hooks[repo].append({"id": hook_id, "url": url, "events": list(events),
                                 "active": True, "content_type": "json"})  # fmt: skip


def _check(name: str, ok: bool = True) -> preflight.Check:
    return preflight.Check(name, ok, "고치는 방법", "docker")


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    directory = tmp_path / "config"
    monkeypatch.setenv(config.CONFIG_DIR_ENV, str(directory))
    github = FakeGitHub()
    state = {"claude": [True], "smee_calls": 0, "compose": 0}

    def smee_new() -> str:
        state["smee_calls"] += 1
        return SMEE

    def claude_ok() -> bool:
        return state["claude"].pop(0) if len(state["claude"]) > 1 else state["claude"][0]

    def compose() -> bool:
        state["compose"] += 1
        return True

    deps = flow.Deps(
        which=lambda name: True, docker_ok=lambda: True, runner=github.runner,
        claude_ok=claude_ok, redis_ok=lambda url: True, compose_up=compose,
        port_busy=lambda port: False, pr_proof_on=lambda port: False, smee_new=smee_new,
        checks=lambda url: [_check("GitHub 인증"), _check("Claude 로그인"), _check("Docker"),
                            _check("Redis")],
        new_secret=lambda: "fake-secret-0001", environ={}, platform="darwin", hints=HINTS,
    )  # fmt: skip
    return {"dir": directory, "gh": github, "deps": deps, "state": state}


def _io(answers=(), secrets=(), interactive=False):
    lines: list[str] = []
    asked = list(answers)
    secret_answers = list(secrets)
    io = flow.Io(
        interactive=interactive, say=lines.append,
        ask=lambda prompt: (lines.append(prompt), asked.pop(0))[1],
        ask_secret=lambda prompt: (lines.append(prompt), secret_answers.pop(0))[1],
    )  # fmt: skip
    return io, lines


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _configure(env, interactive=False, answers=(), secrets=(), **opts):
    io, lines = _io(answers, secrets, interactive)
    code = flow.configure(flow.Options(**opts), io, env["deps"], env["dir"])
    return code, "\n".join(lines)


# --- configure ---


def test_configure_without_questions_sets_everything_up(env) -> None:
    code, out = _configure(env, repos=[REPO], auth="gh")
    assert code == 0, out
    d = env["dir"]
    assert yaml.safe_load((d / "config.yaml").read_text())["repos"] == [REPO]
    for name in ("webhook_secret", "webhook_url"):
        assert _mode(d / name) == 0o600
    assert (d / "webhook_url").read_text().strip() == SMEE
    assert files.read_port(d) == 8765
    [post] = env["gh"].payloads
    assert post["events"] == list(pure.HOOK_EVENTS)
    assert post["config"] == {"url": SMEE, "content_type": "json", "secret": "fake-secret-0001",
                              "insecure_ssl": "0"}  # fmt: skip
    assert "PR 소식이 smee.io를 거쳐" in out
    for name in ("GitHub 인증", "Claude 로그인", "Docker", "Redis"):
        assert name in out
    # 끝 안내는 실행한 방식에 맞는 명령 하나다 (테스트는 clone한 폴더에서 돈다)
    assert "다음: npx . start\n" in out and "OPERATIONS.md" in out
    assert "fake-secret-0001" not in out


def test_configure_again_changes_nothing_and_does_not_duplicate(env) -> None:
    _configure(env, repos=[REPO], auth="gh")
    d = env["dir"]
    before = {n: (d / n).read_text() for n in ("config.yaml", "webhook_secret", "webhook_url")}
    code, out = _configure(env, repos=[REPO], auth="gh")
    assert code == 0, out
    assert {n: (d / n).read_text() for n in before} == before
    assert env["state"]["smee_calls"] == 1  # 저장한 주소를 다시 쓴다
    assert len(env["gh"].hooks[REPO]) == 1  # 같은 주소의 웹훅은 만들지 않고 맞춘다
    assert [w[2] for w in env["gh"].writes] == ["POST", "PATCH"]
    assert "이미 허용 목록에 있음" in out


def test_configure_keeps_existing_settings_and_comments(env) -> None:
    d = env["dir"]
    d.mkdir()
    original = (
        "# 내 설정\nredis_url: redis://127.0.0.1:6390/0  # 따로\nrepos: [octocat/spoon-knife]\n"
    )
    (d / "config.yaml").write_text(original)
    files.write_private(d / "webhook_secret", "kept-secret")
    code, out = _configure(env, repos=[REPO], auth="gh")
    assert code == 0, out
    text = (d / "config.yaml").read_text()
    assert text.startswith("# 내 설정\nredis_url: redis://127.0.0.1:6390/0  # 따로\n")
    assert yaml.safe_load(text)["repos"] == [OTHER, REPO]
    assert (d / "webhook_secret").read_text().strip() == "kept-secret"  # 덮어쓰지 않는다
    assert {p["config"]["secret"] for p in env["gh"].payloads} == {"kept-secret"}
    # 웹훅은 목록의 모든 repo에 맞춘다
    assert len(env["gh"].hooks[OTHER]) == 1 and len(env["gh"].hooks[REPO]) == 1


def test_non_interactive_without_repo_says_what_is_missing(env) -> None:
    code, out = _configure(env, auth="gh")
    assert code == 1
    assert "--repo" in out and "octocat/hello-world" in out
    assert not (env["dir"] / "config.yaml").exists()


def test_bad_repo_name_is_explained(env) -> None:
    code, out = _configure(env, repos=["hello-world"], auth="gh")
    assert code == 1 and "repo는 `계정/저장소이름` 꼴이나 GitHub 주소로 적습니다" in out


def test_unreadable_repo_is_not_added(env) -> None:
    code, out = _configure(env, repos=["octocat/nope"], auth="gh")
    assert code == 1 and "찾지 못했거나 읽을 권한이 없습니다" in out
    assert not (env["dir"] / "config.yaml").exists()


def test_token_from_env_is_checked_then_saved_600_and_never_printed(env) -> None:
    env["deps"].environ = {gh.TOKEN_INPUT_ENV: TOKEN}
    code, out = _configure(env, repos=[REPO], auth="token")
    assert code == 0, out
    path = env["dir"] / github_auth.TOKEN_FILE
    assert path.read_text().strip() == TOKEN and _mode(path) == 0o600
    assert TOKEN in env["gh"].tokens_seen
    assert TOKEN not in out


def test_rejected_token_is_not_saved(env) -> None:
    env["deps"].environ = {gh.TOKEN_INPUT_ENV: "github_pat_WRONG"}
    code, out = _configure(env, repos=[REPO], auth="token")
    assert code == 1 and "만료·폐기" in out
    assert not (env["dir"] / github_auth.TOKEN_FILE).exists()
    assert "github_pat_WRONG" not in out


def test_interactive_token_is_asked_hidden_again_after_a_bad_one(env) -> None:
    code, out = _configure(env, interactive=True, repos=[REPO], auth="token", answers=[""],
                           secrets=["github_pat_WRONG", TOKEN])  # fmt: skip
    assert code == 0, out
    assert "Contents: Read and write" in out  # 묻기 전에 필요한 권한을 보인다
    assert (env["dir"] / github_auth.TOKEN_FILE).read_text().strip() == TOKEN


def test_token_without_input_non_interactive_fails(env) -> None:
    code, out = _configure(env, repos=[REPO], auth="token")
    assert code == 1 and gh.TOKEN_INPUT_ENV in out and "명령줄 인자로는 받지 않습니다" in out


def test_gh_choice_with_token_file_present_stops(env) -> None:
    env["dir"].mkdir()
    files.write_private(env["dir"] / github_auth.TOKEN_FILE, TOKEN)
    code, out = _configure(env, repos=[REPO], auth="gh")
    assert code == 1 and "토큰 방식이 앞섭니다" in out


def test_missing_programs_are_shown_all_at_once(env) -> None:
    env["deps"].which = lambda name: name not in ("gh", "claude")
    code, out = _configure(env, repos=[REPO], auth="gh")
    assert code == 1
    assert "brew install gh" in out and "claude를 실행해 /login 까지 해야 한다" in out


def test_docker_off_says_to_start_it(env) -> None:
    env["deps"].docker_ok = lambda: False
    code, out = _configure(env, repos=[REPO], auth="gh")
    assert code == 1 and "Docker가 꺼져 있습니다" in out


def test_claude_not_logged_in_says_how_and_stops_without_terminal(env) -> None:
    env["state"]["claude"] = [False]
    code, out = _configure(env, repos=[REPO], auth="gh")
    assert code == 1
    assert "claude를 실행해 /login" in out and "다시 실행하세요" in out


def test_claude_login_is_checked_again_on_enter(env) -> None:
    env["state"]["claude"] = [False, True]
    code, out = _configure(env, interactive=True, repos=[REPO], auth="gh", answers=["", ""])
    assert code == 0, out
    assert "로그인한 뒤 Enter를 누르면 다시 확인합니다" in out


def test_busy_port_needs_another_number_without_terminal(env) -> None:
    env["deps"].port_busy = lambda port: port == 8765
    code, out = _configure(env, repos=[REPO], auth="gh")
    assert code == 1 and "--port" in out
    code, out = _configure(env, repos=[REPO], auth="gh", port=8799)
    assert code == 0, out
    assert files.read_port(env["dir"]) == 8799


def test_busy_port_by_this_setups_own_server_is_fine(env) -> None:
    """이 설정이 이미 쓰던 포트에서 pr-proof가 돌고 있으면 다시 돌린 configure의 자기 서버다."""
    files.write_port(8765, env["dir"])
    env["deps"].port_busy = lambda port: True
    env["deps"].pr_proof_on = lambda port: True
    code, _ = _configure(env, repos=[REPO], auth="gh")
    assert code == 0


def test_another_pr_proof_on_the_port_is_busy(env) -> None:
    """저장한 적 없는 포트에서 도는 pr-proof는 다른 설정의 서버다. 자기 것으로 보지 않는다."""
    env["deps"].port_busy = lambda port: port == 8765
    env["deps"].pr_proof_on = lambda port: port == 8765
    code, out = _configure(env, repos=[REPO], auth="gh")
    assert code == 1 and "포트 8765를 다른 pr-proof 서버가 쓰고 있습니다" in out
    code, out = _configure(env, repos=[REPO], auth="gh", port=8799)
    assert code == 0, out
    assert files.read_port(env["dir"]) == 8799


def test_repo_is_saved_as_github_spells_it(env) -> None:
    """소문자로 적거나 주소를 붙여 넣어도 GitHub에 적힌 이름으로 넣는다. 웹훅 소식에는 그 이름이
    오고, 다르면 서버가 허용 목록에 없다고 버린다."""
    code, out = _configure(env, repos=[REPO.upper(), f"https://github.com/{OTHER}.git"], auth="gh")
    assert code == 0, out
    assert yaml.safe_load((env["dir"] / "config.yaml").read_text())["repos"] == [REPO, OTHER]
    assert f"{REPO.upper()} → {REPO}" in out
    assert {tuple(sorted(env["gh"].hooks))} == {tuple(sorted([REPO, OTHER]))}


def test_next_command_matches_how_it_was_run(env, monkeypatch) -> None:
    monkeypatch.setattr(files, "in_clone", lambda: False)
    code, out = _configure(env, repos=[REPO], auth="gh")
    assert code == 0 and "다음: npx pr-proof start\n" in out and "npx . start" not in out


def test_redis_down_is_started_with_compose(env) -> None:
    calls = iter([False, True])
    env["deps"].redis_ok = lambda url: next(calls)
    code, out = _configure(env, repos=[REPO], auth="gh")
    assert code == 0, out
    assert env["state"]["compose"] == 1 and "Redis를 띄웠습니다" in out


def test_no_hook_permission_shows_manual_values(env) -> None:
    env["gh"].hook_admin = False
    code, out = _configure(env, repos=[REPO], auth="gh")
    assert code == 1
    assert "Add webhook에서 손으로 등록하세요" in out and SMEE in out
    assert "webhook_secret 파일의 값" in out and "fake-secret-0001" not in out


def test_manual_webhook_option_does_not_call_hook_api(env) -> None:
    code, out = _configure(env, repos=[REPO], auth="gh", manual_webhook=True)
    assert code == 0, out
    assert env["gh"].writes == [] and "손으로 등록하세요" in out


def test_given_webhook_url_is_used_and_saved_without_smee(env) -> None:
    url = "https://box.example.com/webhook/github"
    code, _ = _configure(env, repos=[REPO], auth="gh", webhook_url=url)
    assert code == 0
    assert env["state"]["smee_calls"] == 0
    assert (env["dir"] / "webhook_url").read_text().strip() == url
    assert env["gh"].payloads[0]["config"]["url"] == url


def test_existing_other_pr_proof_hook_is_not_duplicated(env) -> None:
    env["gh"].hook(REPO, "https://smee.io/OLDchannel")
    code, out = _configure(env, repos=[REPO], auth="gh")
    assert code == 1
    assert env["gh"].writes == [] and "https://smee.io/OLDchannel" in out


def test_dry_run_writes_nothing(env) -> None:
    code, out = _configure(env, repos=[REPO], auth="gh", dry_run=True)
    assert code == 0, out
    assert not env["dir"].exists()
    assert env["gh"].writes == [] and env["state"]["smee_calls"] == 0
    assert "(dry-run)" in out and "웹훅을 만든다" in out


def test_interactive_questions(env) -> None:
    # 인증 방식(Enter: gh 로그인) → repo(틀린 것, 맞는 것) → 웹훅 받는 주소(Enter: smee)
    code, out = _configure(env, interactive=True, answers=["", "bad", f"{REPO} {OTHER}", ""])
    assert code == 0, out
    assert "repo는 `계정/저장소이름` 꼴이나 GitHub 주소로 적습니다" in out
    assert yaml.safe_load((env["dir"] / "config.yaml").read_text())["repos"] == [REPO, OTHER]
    assert "pr-proof가 도는 이 컴퓨터로 들어오는" in out and "repo가 공개인지와 상관없다" in out


def test_failed_final_check_is_shown_and_fails(env) -> None:
    env["deps"].checks = lambda url: [_check("GitHub 인증"), _check("Claude 로그인", ok=False)]
    code, out = _configure(env, repos=[REPO], auth="gh")
    assert code == 1
    assert "Claude 로그인" in out and "실패 → 고치는 방법" in out


# --- add ---


def _add(env, repos, **opts):
    io, lines = _io()
    code = flow.add(flow.Options(repos=repos, **opts), io, env["deps"], env["dir"])
    return code, "\n".join(lines)


def test_add_before_configure_says_run_configure(env) -> None:
    code, out = _add(env, [REPO])
    assert code == 1 and "먼저 npx pr-proof configure" in out


def test_add_registers_repo_and_webhook_and_says_restart(env) -> None:
    _configure(env, repos=[REPO], auth="gh")
    code, out = _add(env, [OTHER])
    assert code == 0, out
    assert yaml.safe_load((env["dir"] / "config.yaml").read_text())["repos"] == [REPO, OTHER]
    assert env["gh"].hooks[OTHER][0]["url"] == SMEE
    assert flow.RESTART in out


def test_add_existing_repo_only_fixes_the_webhook(env) -> None:
    _configure(env, repos=[REPO], auth="gh")
    code, out = _add(env, [REPO])
    assert code == 0
    assert "이미 허용 목록에 있음" in out and flow.RESTART not in out
    assert len(env["gh"].hooks[REPO]) == 1


def test_add_dry_run(env) -> None:
    _configure(env, repos=[REPO], auth="gh")
    before = (env["dir"] / "config.yaml").read_text()
    writes = len(env["gh"].writes)
    code, out = _add(env, [OTHER], dry_run=True)
    assert code == 0 and "(dry-run)" in out
    assert (env["dir"] / "config.yaml").read_text() == before and len(env["gh"].writes) == writes


def test_add_rejects_bad_name(env) -> None:
    code, out = _add(env, ["nope"])
    assert code == 1 and "예: octocat/hello-world" in out


# --- rm ---


def _rm(env, repos, interactive=False, answers=(), **opts):
    io, lines = _io(answers, interactive=interactive)
    code = flow.remove(flow.Options(repos=repos, **opts), io, env["deps"], env["dir"])
    return code, "\n".join(lines)


def test_rm_removes_repo_and_only_our_webhook(env) -> None:
    _configure(env, repos=[REPO, OTHER], auth="gh")
    env["gh"].hook(REPO, "https://other.example/hook", hook_id=5)
    code, out = _rm(env, [REPO], yes=True)
    assert code == 0, out
    assert yaml.safe_load((env["dir"] / "config.yaml").read_text())["repos"] == [OTHER]
    assert [h["url"] for h in env["gh"].hooks[REPO]] == ["https://other.example/hook"]
    assert flow.LEFT_ALONE in out and flow.RESTART in out


def test_rm_last_repo_leaves_empty_list(env) -> None:
    _configure(env, repos=[REPO], auth="gh")
    code, _ = _rm(env, [REPO], yes=True)
    assert code == 0
    assert config.load_config(env["dir"]).repos == []


def test_rm_unknown_repo_does_nothing(env) -> None:
    _configure(env, repos=[REPO], auth="gh")
    before = (env["dir"] / "config.yaml").read_text()
    code, out = _rm(env, [OTHER], yes=True)
    assert code == 0 and "허용 목록에 없습니다" in out
    assert (env["dir"] / "config.yaml").read_text() == before


def test_rm_asks_once_on_terminal_and_refuses_without_yes_elsewhere(env) -> None:
    _configure(env, repos=[REPO], auth="gh")
    code, out = _rm(env, [REPO])
    assert code == 1 and "--yes" in out
    code, out = _rm(env, [REPO], interactive=True, answers=["n"])
    assert code == 1 and "그만둡니다" in out
    assert config.load_config(env["dir"]).repos == [REPO]
    code, _ = _rm(env, [REPO], interactive=True, answers=["y"])
    assert code == 0 and config.load_config(env["dir"]).repos == []


def test_rm_without_hook_permission_still_removes_from_list(env) -> None:
    _configure(env, repos=[REPO], auth="gh")
    env["gh"].hook_admin = False
    code, out = _rm(env, [REPO], yes=True)
    assert code == 1
    assert config.load_config(env["dir"]).repos == []
    assert "웹훅은 손으로 지우세요" in out and SMEE in out


def test_rm_dry_run(env) -> None:
    _configure(env, repos=[REPO], auth="gh")
    code, out = _rm(env, [REPO], dry_run=True)
    assert code == 0 and "(dry-run)" in out
    assert config.load_config(env["dir"]).repos == [REPO]
    assert len(env["gh"].hooks[REPO]) == 1


# --- list ---


def _list(env, **opts):
    io, lines = _io()
    code = flow.list_repos(flow.Options(**opts), io, env["deps"], env["dir"])
    return code, lines


def test_list_reads_only_the_config_file(env) -> None:
    _configure(env, repos=[REPO, OTHER], auth="gh")

    def no_github(token):
        raise AssertionError("list는 GitHub을 부르지 않는다")

    env["deps"].runner = no_github
    assert _list(env) == (0, [REPO, OTHER])


def test_list_empty_suggests_add(env) -> None:
    env["dir"].mkdir()
    (env["dir"] / "config.yaml").write_text("repos: []\n")
    code, lines = _list(env)
    assert code == 0 and "npx pr-proof add <repo>" in lines[0]


def test_list_check_shows_hook_state_and_survives_failures(env) -> None:
    _configure(env, repos=[REPO, OTHER], auth="gh")
    env["gh"].hooks[OTHER][0]["events"] = ["pull_request"]
    code, lines = _list(env, check=True)
    assert code == 0
    assert lines == [f"{REPO}  웹훅 있음, 이벤트 맞음",
                     f"{OTHER}  웹훅 있음, 이벤트 빠짐: Issue comments, Pull request reviews, "
                     "Pull request review comments"]  # fmt: skip
    env["gh"].hook_admin = False
    code, lines = _list(env, check=True)
    assert code == 0 and all("권한 없음" in line for line in lines)


def test_add_and_rm_ignore_case_and_take_addresses(env) -> None:
    """add는 GitHub에 적힌 이름으로 넣고, rm은 대소문자를 다르게 적거나 주소를 붙여 넣어도 뺀다."""
    assert _configure(env, repos=[REPO], auth="gh")[0] == 0
    code, out = _add(env, [f"https://github.com/{OTHER.upper()}"])
    assert code == 0, out
    assert yaml.safe_load((env["dir"] / "config.yaml").read_text())["repos"] == [REPO, OTHER]
    assert [h["url"] for h in env["gh"].hooks[OTHER]] == [SMEE]
    # 이미 있는 repo를 다른 대소문자로 더해도 줄이 늘지 않는다
    assert _add(env, [REPO.upper()])[0] == 0
    assert yaml.safe_load((env["dir"] / "config.yaml").read_text())["repos"] == [REPO, OTHER]
    code, out = _rm(env, [f"git@github.com:{OTHER.upper()}.git"], yes=True)
    assert code == 0, out
    assert yaml.safe_load((env["dir"] / "config.yaml").read_text())["repos"] == [REPO]
    assert env["gh"].hooks[OTHER] == []
