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
from app.shared import config, github_auth, server
from app.shared.comment_pure import COMMENT_MARKER, result_marker
from app.shared.embed_models_pure import E5_SMALL, EmbedModel
from app.worker import preflight

REPO: Final = "octocat/hello-world"
OTHER: Final = "octocat/spoon-knife"
SMEE: Final = "https://smee.io/FAKEnewchannel"
TOKEN: Final = "github_pat_FAKE2222222222222222222222"
HEAD: Final = "a" * 40
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
        # 열린 PR 번호와 PR마다 결과 코멘트 (작성자, 고친 때, 첫 줄)
        self.pulls: dict[str, list[int]] = {}
        self.result_comments: dict[tuple[str, int], list[tuple[str, str, str]]] = {}
        self.pulls_error = False

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
        if "/pulls?state=open" in path:
            if self.pulls_error:
                raise gh.SetupGitHubError("gh api repos 실패: HTTP 502")
            return "".join(f"{n}\n" for n in self.pulls.get(repo, []))
        if path.endswith("/comments"):
            number = int(path.split("/")[-2])
            rows = self.result_comments.get((repo, number), [])
            return "".join("\t".join(row) + "\n" for row in rows)
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


class FakeRag:
    """pip과 임베딩 모델 받기 대신. 깐 것과 받은 것을 적기만 한다(파일을 쓰지 않는다)."""

    def __init__(self) -> None:
        self.installed = False
        self.install_ok = True
        self.installs = 0
        self.fetched: list[tuple[Path, str]] = []
        self.fetch_error: Exception | None = None

    def install(self) -> bool:
        self.installs += 1
        self.installed = self.install_ok
        return self.install_ok

    def fetch(self, model_dir: Path, spec: EmbedModel) -> Path:
        if self.fetch_error is not None:
            raise self.fetch_error
        self.fetched.append((model_dir, spec.name))
        return model_dir

    def deps(self) -> flow.RagDeps:
        return flow.RagDeps(
            install=self.install, extras_missing=lambda: not self.installed,
            fetch_model=self.fetch,
            model_ready=lambda model_dir, spec: (model_dir, spec.name) in self.fetched,
        )  # fmt: skip


def _check(name: str, ok: bool = True) -> preflight.Check:
    return preflight.Check(name, ok, "고치는 방법", "docker")


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    directory = tmp_path / "config"
    monkeypatch.setenv(config.CONFIG_DIR_ENV, str(directory))
    monkeypatch.delenv(files.COMMAND_ENV, raising=False)  # 입구가 넘기는 값. 테스트마다 정한다
    github = FakeGitHub()
    rag = FakeRag()
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
        rag=rag.deps(),
        new_secret=lambda: "fake-secret-0001", environ={}, platform="darwin", hints=HINTS,
    )  # fmt: skip
    return {"dir": directory, "gh": github, "deps": deps, "state": state, "rag": rag}


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
    # 묻는 것: 모델(Enter), 검색(Enter), 웹훅 받는 주소(Enter)
    code, out = _configure(env, interactive=True, repos=[REPO], auth="token",
                           answers=["", "", ""],
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
    # 묻는 것: Claude 로그인 뒤 Enter, 모델(Enter), 검색(Enter), 웹훅 받는 주소(Enter)
    code, out = _configure(env, interactive=True, repos=[REPO], auth="gh",
                           answers=["", "", "", ""])  # fmt: skip
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


@pytest.mark.parametrize("command", ["pr-proof", "npx pr-proof", "npx ."])
def test_next_command_matches_how_it_was_run(env, monkeypatch, command: str) -> None:
    """입구(bin/pr-proof.js)가 부른 방식을 PR_PROOF_COMMAND로 넘기면 안내가 그 방식이다."""
    monkeypatch.setenv(files.COMMAND_ENV, command)
    code, out = _configure(env, repos=[REPO], auth="gh")
    assert code == 0 and f"다음: {command} start\n" in out
    # 파일에 남는 글은 부른 방식과 상관없이 `pr-proof <명령>`이다
    text = (env["dir"] / files.CONFIG_FILE).read_text()
    assert text.startswith("# pr-proof 설정 (configure가 만들었다). repos는 pr-proof add / rm으로")
    assert (env["dir"] / files.RUN_ENV_FILE).read_text().startswith("# pr-proof start가")


def test_next_command_without_the_entry_follows_the_folder(env, monkeypatch) -> None:
    """`python -m app.setup.cli`로 직접 부르면 clone한 폴더는 `npx .`, 아니면 `pr-proof`다."""
    monkeypatch.setattr(files, "in_clone", lambda: False)
    code, out = _configure(env, repos=[REPO], auth="gh")
    assert code == 0 and "다음: pr-proof start\n" in out and "npx" not in out


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
    # 인증 방식(Enter: gh 로그인) → 모델(Enter: 그대로) → 검색(Enter: 아니오) →
    # repo(틀린 것, 맞는 것) → 웹훅 받는 주소(Enter: smee)
    code, out = _configure(env, interactive=True,
                           answers=["", "", "", "bad", f"{REPO} {OTHER}", ""])  # fmt: skip
    assert code == 0, out
    assert "model" not in yaml.safe_load((env["dir"] / "config.yaml").read_text())
    assert "repo는 `계정/저장소이름` 꼴이나 GitHub 주소로 적습니다" in out
    assert yaml.safe_load((env["dir"] / "config.yaml").read_text())["repos"] == [REPO, OTHER]
    assert "pr-proof가 도는 이 컴퓨터로 들어오는" in out and "repo가 공개인지와 상관없다" in out


def test_failed_final_check_is_shown_and_fails(env) -> None:
    env["deps"].checks = lambda url: [_check("GitHub 인증"), _check("Claude 로그인", ok=False)]
    code, out = _configure(env, repos=[REPO], auth="gh")
    assert code == 1
    assert "Claude 로그인" in out and "실패 → 고치는 방법" in out


def test_steps_are_numbered_out_of_ten_with_the_model_and_search_after_claude(env) -> None:
    code, out = _configure(env, repos=[REPO], auth="gh")
    assert code == 0, out
    steps = [line for line in out.splitlines() if line.startswith("[")]
    assert [s.split("]")[0] for s in steps] == [f"[{n}/10" for n in range(1, 11)]
    assert steps[4] == "[5/10] Claude 로그인" and steps[5] == "[6/10] 모델"
    assert steps[6] == "[7/10] 과거 수정 기록 검색"


def test_without_model_option_nothing_is_asked_or_written(env) -> None:
    code, out = _configure(env, repos=[REPO], auth="gh")
    assert code == 0, out
    assert "모델: 기본값 (판단 sonnet, 수정 haiku→sonnet) (바꾸려면 --model <이름>)" in out
    assert "model" not in yaml.safe_load((env["dir"] / "config.yaml").read_text())
    assert flow.SAVED_JUDGMENTS not in out


def test_model_option_sets_every_stage_and_keeps_the_rest(env) -> None:
    code, out = _configure(env, repos=[REPO], auth="gh", model="opus")
    assert code == 0, out
    loaded = config.load_config(env["dir"])
    assert (loaded.model.default, loaded.repos) == ("opus", [REPO])
    assert "모델: opus" in out and flow.SAVED_JUDGMENTS in out
    # 다시 돌려도 같다. default를 주면 지운다
    code, out = _configure(env, repos=[REPO], auth="gh", model="opus")
    assert code == 0 and "모델: opus (그대로)" in out and flow.SAVED_JUDGMENTS not in out
    code, out = _configure(env, repos=[REPO], auth="gh", model="default")
    assert code == 0 and config.load_config(env["dir"]).model.default is None


def test_bad_model_option_is_refused_before_anything(env) -> None:
    code, out = _configure(env, repos=[REPO], auth="gh", model="-x")
    assert code == 1 and "모델 이름의 꼴이 아닙니다" in out
    assert not env["dir"].exists()


def test_model_option_in_dry_run_writes_nothing(env) -> None:
    code, out = _configure(env, repos=[REPO], auth="gh", model="opus", dry_run=True)
    assert code == 0, out
    assert not env["dir"].exists()
    assert "(dry-run)" in out and "model.default를 opus로 적는다" in out


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        (["3"], "opus"),
        (["9", "4"], "haiku"),  # 없는 번호는 다시 묻는다
        (["5", "-bad", "claude-sonnet-4-5"], "claude-sonnet-4-5"),  # 직접 입력, 틀린 꼴은 다시
    ],
)
def test_interactive_model_choice(env, answers: list[str], expected: str) -> None:
    code, out = _configure(env, interactive=True, answers=["", *answers, "", REPO, ""])
    assert code == 0, out
    assert config.load_config(env["dir"]).model.default == expected
    assert "1) 기본값 (판단 sonnet, 수정 haiku→sonnet)" in out and "5) 직접 입력" in out


def test_interactive_model_shows_the_saved_one_and_enter_keeps_it(env) -> None:
    _configure(env, repos=[REPO], auth="gh", model="opus")
    code, out = _configure(env, interactive=True, answers=["", "", ""])
    assert code == 0, out
    assert "지금: opus" in out and "모델: opus (그대로)" in out
    assert config.load_config(env["dir"]).model.default == "opus"
    code, out = _configure(env, interactive=True, answers=["", "1", ""])  # 1: 기본값으로
    assert code == 0 and config.load_config(env["dir"]).model.default is None


# --- configure: 과거 수정 기록 검색 ---


def _rag_allowed(env) -> bool | None:
    return config.load_config(env["dir"]).rag.allowed


def test_without_rag_option_nothing_is_asked_installed_or_written(env) -> None:
    code, out = _configure(env, repos=[REPO], auth="gh")
    assert code == 0, out
    assert "과거 수정 기록 검색: 정하지 않음" in out and "--rag on" in out
    assert _rag_allowed(env) is None
    assert (env["rag"].installs, env["rag"].fetched) == (0, [])


def test_rag_on_installs_the_extras_fetches_the_model_and_saves_the_choice(env) -> None:
    code, out = _configure(env, repos=[REPO], auth="gh", rag="on")
    assert code == 0, out
    rag = env["rag"]
    data_dir = config.load_config(env["dir"]).data_dir
    assert rag.installs == 1 and rag.fetched == [(data_dir / "models", E5_SMALL)]
    assert "pr-proof[rag]를 깝니다 (패키지 수백 MB" in out and "(약 487MB)" in out
    assert _rag_allowed(env) is True and "과거 수정 기록 검색: 켬" in out
    # 다시 돌려도 같다: 깔려 있으면 다시 깔지 않는다
    code, out = _configure(env, repos=[REPO], auth="gh", rag="on")
    assert code == 0 and rag.installs == 1 and "pr-proof[rag]: 깔려 있음" in out
    code, out = _configure(env, repos=[REPO], auth="gh", rag="off")
    assert code == 0 and _rag_allowed(env) is False and "과거 수정 기록 검색: 끔" in out
    code, out = _configure(env, repos=[REPO], auth="gh", rag="off")
    assert code == 0 and "끔 (그대로)" in out


def test_rag_on_that_fails_to_install_saves_nothing_and_says_how_to_retry(env) -> None:
    env["rag"].install_ok = False
    code, out = _configure(env, repos=[REPO], auth="gh", rag="on")
    assert code == 1
    assert "깔지 못했습니다" in out and "rag on" in out
    assert _rag_allowed(env) is None and env["rag"].fetched == []
    assert yaml.safe_load((env["dir"] / "config.yaml").read_text())["repos"] == [REPO]  # 이어 간다


def test_rag_on_in_dry_run_installs_and_writes_nothing(env) -> None:
    code, out = _configure(env, repos=[REPO], auth="gh", rag="on", dry_run=True)
    assert code == 0, out
    assert not env["dir"].exists()
    assert (env["rag"].installs, env["rag"].fetched) == (0, [])
    assert "(dry-run)" in out and "rag.allowed를 true로 적는다" in out


def test_interactive_rag_question_defaults_to_no_and_tells_the_size(env) -> None:
    # 인증 방식, 모델, 검색(Enter: 아니오), repo, 웹훅 받는 주소
    code, out = _configure(env, interactive=True, answers=["", "", "", REPO, ""])
    assert code == 0, out
    assert "과거 수정 기록 검색을 쓸까요? [y/N]: " in out
    assert "패키지 수백 MB" in out and "임베딩 모델(약 487MB)" in out
    assert _rag_allowed(env) is False and env["rag"].installs == 0
    # 켬이면 Enter는 그대로 켬이다
    code, out = _configure(env, interactive=True, answers=["", "", "maybe", "y"])
    assert code == 0 and "y나 n을 적습니다" in out and _rag_allowed(env) is True
    code, out = _configure(env, interactive=True, answers=["", "", ""])
    assert code == 0 and "[Y/n]" in out and _rag_allowed(env) is True


# --- add ---


def _add(env, repos, **opts):
    io, lines = _io()
    code = flow.add(flow.Options(repos=repos, **opts), io, env["deps"], env["dir"])
    return code, "\n".join(lines)


def test_add_before_configure_says_run_configure(env) -> None:
    code, out = _add(env, [REPO])
    assert code == 1 and "먼저 npx . configure" in out  # 테스트는 clone한 폴더에서 돈다


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
    assert code == 0 and "npx . add <repo>" in lines[0]


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


def test_list_check_shows_other_servers_in_open_prs(env, tmp_path: Path) -> None:
    """이 계정이 단 결과 코멘트의 표시에서 다른 서버를 찾는다. 이 서버의 표시, 서버 정보가 없는 옛
    표시, 다른 계정이 흉내 낸 표시는 세지 않는다."""
    _configure(env, repos=[REPO, OTHER], auth="gh")
    data = tmp_path / "data"
    with (env["dir"] / "config.yaml").open("a") as f:
        f.write(f"data_dir: {data}\n")
    mine = server.load_id(data)
    env["gh"].hook(REPO, SMEE)
    env["gh"].hook(OTHER, SMEE)
    env["gh"].pulls = {REPO: [12, 11]}
    env["gh"].result_comments = {
        (REPO, 12): [("me", "2026-11-24T10:05:00Z", result_marker("bbbb2222", "0.2.0", HEAD)),
                     ("someone", "2026-11-24T11:00:00Z", result_marker("dddd4444", "9.0.0", HEAD))],
        (REPO, 11): [("me", "2026-11-23T09:00:00Z", result_marker("bbbb2222", "0.1.9", HEAD)),
                     ("me", "2026-11-23T09:30:00Z", result_marker(mine, "0.1.3", HEAD)),
                     ("me", "2026-11-23T09:40:00Z", COMMENT_MARKER)],
    }  # fmt: skip
    code, lines = _list(env, check=True)
    assert code == 0
    assert lines == [
        f"{REPO}  웹훅 있음, 이벤트 맞음",
        f"{REPO}  다른 pr-proof 서버가 처리하고 있음: 서버 bbbb2222 (판 0.2.0, 마지막 처리 "
        f"2026-11-24 10:05 UTC PR #12). 한 서버에서 npx . rm {REPO}로 빼세요",
        f"{OTHER}  웹훅 있음, 이벤트 맞음",
    ]
    assert [p.name for p in data.iterdir()] == [server.ID_FILE]  # list는 데이터 폴더에 쓰지 않는다
    env["gh"].pulls_error = True
    code, lines = _list(env, check=True)
    assert code == 0 and f"{OTHER}  다른 pr-proof 서버를 확인하지 못함 (GitHub 조회 실패)" in lines


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


# --- model ---


def _model(env, name=None):
    io, lines = _io()
    code = flow.model(name, io, env["dir"])
    return code, "\n".join(lines)


def test_model_before_configure_says_run_configure(env) -> None:
    code, out = _model(env)
    assert code == 1 and "configure를 실행하세요" in out


def test_model_shows_sets_and_resets_keeping_other_settings(env) -> None:
    d = env["dir"]
    d.mkdir()
    head = "# 내 설정\nrepos: [octocat/spoon-knife]  # 목록\n"
    original = head + "model:\n  calls:\n    max_pr_max: 7\n"
    (d / "config.yaml").write_text(original)
    code, out = _model(env)
    assert code == 0 and "파이프라인 쪽 모델: 정하지 않음" in out and "이 값보다 앞섭니다" in out

    code, out = _model(env, "opus")
    assert code == 0, out
    assert "모든 단계의 모델을 opus로 정했습니다" in out
    assert flow.SAVED_JUDGMENTS in out and flow.RESTART in out
    loaded = config.load_config(d)
    assert (loaded.model.default, loaded.model.calls.max_pr_max) == ("opus", 7)
    assert (d / "config.yaml").read_text().startswith(head)

    code, out = _model(env)
    assert code == 0 and "파이프라인 쪽 모델: opus" in out
    code, out = _model(env, "opus")
    assert code == 0 and "이미 opus입니다" in out and flow.RESTART not in out

    code, out = _model(env, "default")
    assert code == 0 and "단계별 기본값(판단 sonnet, 수정 haiku→sonnet)으로 되돌렸습니다" in out
    assert (d / "config.yaml").read_text() == original
    code, out = _model(env, "default")
    assert code == 0 and "이미 기본값" in out


@pytest.mark.parametrize("name", ["-x", "opus 4", "a/b"])
def test_model_refuses_a_bad_name_with_examples(env, name: str) -> None:
    env["dir"].mkdir()
    (env["dir"] / "config.yaml").write_text("repos: []\n")
    code, out = _model(env, name)
    assert code == 1 and "모델 이름의 꼴이 아닙니다" in out and "예: sonnet, opus" in out
    assert (env["dir"] / "config.yaml").read_text() == "repos: []\n"


def test_model_view_points_out_a_broken_saved_value(env) -> None:
    env["dir"].mkdir()
    (env["dir"] / "config.yaml").write_text("repos: []\nmodel:\n  default: '-x'\n")
    code, out = _model(env)
    assert code == 1 and "서버가 시작하지 못합니다" in out


def test_model_guidance_uses_the_command_the_user_ran(env, monkeypatch) -> None:
    monkeypatch.setenv(files.COMMAND_ENV, "pr-proof")
    env["dir"].mkdir()
    (env["dir"] / "config.yaml").write_text("repos: []\n")
    code, out = _model(env)
    assert code == 0 and "바꾸기: pr-proof model <이름>" in out and "npx" not in out


# --- rag ---


def _rag(env, action=None):
    io, lines = _io()
    code = flow.rag(action, io, env["rag"].deps(), env["dir"])
    return code, "\n".join(lines)


def _configured(env, text: str = "repos: []\n") -> Path:
    env["dir"].mkdir()
    (env["dir"] / "config.yaml").write_text(text)
    return env["dir"] / "config.yaml"


def test_rag_before_configure_says_run_configure(env) -> None:
    code, out = _rag(env)
    assert code == 1 and "configure를 실행하세요" in out


def test_rag_shows_turns_on_and_off_keeping_other_settings(env, tmp_path: Path) -> None:
    head = f"# 내 설정\nrepos: [octocat/spoon-knife]  # 목록\ndata_dir: {tmp_path / 'data'}\n"
    path = _configured(env, head)
    code, out = _rag(env)
    assert code == 0 and "rag.allowed): 정하지 않음" in out
    assert ": 없음" in out and "받지 않음" in out

    code, out = _rag(env, "on")
    assert code == 0, out
    assert "과거 수정 기록 검색을 켭니다: 선택 의존성 pr-proof[rag](패키지 수백 MB)" in out
    assert env["rag"].fetched == [(tmp_path / "data" / "models", E5_SMALL)]
    assert flow.RESTART in out and path.read_text().startswith(head)
    assert _rag_allowed(env) is True

    code, out = _rag(env)
    assert code == 0 and "rag.allowed): 켬" in out and ": 깔림" in out and "받음 (" in out

    code, out = _rag(env, "off")
    assert code == 0 and "깐 패키지와 받은 임베딩 모델은 그대로 둡니다" in out
    assert _rag_allowed(env) is False and env["rag"].installed  # 지우지 않는다
    code, out = _rag(env)
    assert code == 0 and "rag.allowed): 끔" in out
    code, out = _rag(env, "off")
    assert code == 0 and "이미 끔입니다" in out


def test_rag_on_that_cannot_fetch_the_model_saves_nothing(env) -> None:
    from app.worker.store.memory import SearchUnavailable

    _configured(env)
    env["rag"].fetch_error = SearchUnavailable("onnx/model.onnx: 크기나 해시가 고정한 값과 다름")
    code, out = _rag(env, "on")
    assert code == 1 and "임베딩 모델을 받지 못했습니다" in out and "해시" in out
    assert _rag_allowed(env) is None


def test_rag_sync_installs_again_only_when_turned_on(env) -> None:
    """입구가 실행 환경을 새로 깐 뒤 부른다. 설정이 없거나 켬이 아니면 아무것도 하지 않는다."""
    assert _rag(env, "sync") == (0, "")
    path = _configured(env)
    assert _rag(env, "sync") == (0, "") and env["rag"].installs == 0
    path.write_text("repos: []\nrag:\n  allowed: false\n")
    assert _rag(env, "sync") == (0, "") and env["rag"].installs == 0
    path.write_text("repos: []\nrag:\n  allowed: true\n")
    code, out = _rag(env, "sync")
    assert code == 0 and env["rag"].installs == 1 and "pr-proof[rag]도 깝니다" in out
    env["rag"].install_ok = False
    code, out = _rag(env, "sync")
    assert code == 1 and "검색 없이" in out and "rag on" in out
    path.write_text("repos: [\n")  # 읽지 못하면 본 명령이 알린다
    assert _rag(env, "sync") == (0, "")
