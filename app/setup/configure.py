"""`pr-proof configure | add | rm | list | model`의 흐름.

configure의 단계 (docs/design/08-security.md "설치와 인증"):
1 프로그램 확인 → 2 Docker가 켜져 있는지 → 3 설치(포트, Redis. venv는 입구가 만든다) →
4 GitHub 인증 → 5 Claude 로그인 → 6 모델 → 7 repo 등록과 권한 확인 →
8 웹훅(비밀값, 받는 주소, 등록) → 9 끝 확인 표.

묻는 것은 다섯이다: 인증 방식, 토큰(토큰 방식일 때), 모델, repo, 웹훅 받는 주소. 터미널이 아니면
묻지 않고, 필요한 값이 없으면 무엇이 없는지 말하고 실패한다. 여러 번 돌려도 같다: 목록에 없는 repo만
더하고, 있는 비밀값과 저장한 주소는 덮어쓰지 않는다. `dry_run`이면 파일도 GitHub도 쓰지 않고 할 일만
보인다(GitHub 읽기는 한다).

바깥(프로그램 찾기, gh, Docker, Redis, smee.io, 포트, claude)은 `Deps`로 받는다. 테스트가 바꿔
끼운다.
"""

import getpass
import json
import secrets
import shutil
import socket
import subprocess
import sys
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from redis import Redis
from redis.exceptions import RedisError

from app.setup import files
from app.setup import github_setup as gh
from app.setup import setup_pure as pure
from app.shared import config, github_auth
from app.worker import preflight
from app.worker.sandbox.docker_cli import minimal_env

PROGRAMS: Final = ("docker", "git", "gh", "claude")
OPERATIONS_URL: Final = "https://github.com/Eunryong/pr-proof/blob/main/docs/OPERATIONS.md"
CONFIG_HEADER: Final = (
    f"# pr-proof 설정 (configure가 만들었다). repos는 {files.FILE_COMMAND} add / rm으로, "
    f"model.default는 {files.FILE_COMMAND} model로 고친다.\n"
)
STEPS: Final = 9
RESTART: Final = "도는 서버는 config.yaml을 시작할 때만 읽습니다. 서버가 돌고 있으면 다시 띄우세요."
LEFT_ALONE: Final = (
    "그 repo의 PR 코멘트, 열린 수정안 PR, 데이터 폴더의 기록(DB, 캐시)은 그대로 둡니다."
)
MODEL_CHOICES: Final = ("sonnet", "opus", "haiku")  # configure가 번호로 보이는 이름 (2–4번)
SAVED_JUDGMENTS: Final = (
    "저장한 판단(규칙 읽기, 검사기, 위반 판단, 수정)은 저장 키에 모델이 들어 있어, 모델이 바뀐 "
    "단계는 다음 검사에서 다시 묻습니다 (그만큼 사용량이 듭니다)."
)


@dataclass
class Io:
    interactive: bool
    say: Callable[[str], object] = print
    ask: Callable[[str], str] = input
    ask_secret: Callable[[str], str] = getpass.getpass


@dataclass
class Deps:
    which: Callable[[str], bool]
    docker_ok: Callable[[], bool]
    runner: Callable[[str | None], gh.Runner]
    claude_ok: Callable[[], bool]
    redis_ok: Callable[[str], bool]
    compose_up: Callable[[], bool]
    port_busy: Callable[[int], bool]
    pr_proof_on: Callable[[int], bool]
    smee_new: Callable[[], str]
    checks: Callable[[str], list[preflight.Check]]
    new_secret: Callable[[], str] = lambda: secrets.token_hex(32)
    environ: Mapping[str, str] = field(default_factory=dict)
    platform: str = sys.platform
    hints: Mapping[str, Mapping[str, str]] = field(default_factory=dict)


@dataclass
class Options:
    repos: list[str] = field(default_factory=list)
    auth: str | None = None  # "gh" | "token"
    webhook_url: str | None = None
    port: int | None = None
    dry_run: bool = False
    manual_webhook: bool = False  # 웹훅을 API로 등록하지 않고 손으로 등록할 값만 보인다
    yes: bool = False  # rm: 확인을 묻지 않는다
    # configure: 모든 단계에 쓸 모델. "default"면 지운다. None이면 터미널에서 묻고, 아니면 그대로
    model: str | None = None
    check: bool = False  # list: 웹훅까지 본다


class Stop(Exception):
    """흐름을 멈춘다. 글은 이미 보였다."""


# --- 실제 바깥 ---


def _quiet(args: list[str], timeout: float = 30) -> bool:
    try:
        return subprocess.run(args, capture_output=True, env=minimal_env(), timeout=timeout,
                              check=False).returncode == 0  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return False


def _redis_ok(url: str) -> bool:
    try:
        return bool(Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2).ping())
    except (RedisError, ValueError):
        return False


def _port_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _pr_proof_on(port: int) -> bool:
    """그 포트에서 도는 것이 pr-proof api인지 (`/healthz`). 다시 돌린 configure가 자기 서버를 다른
    프로그램으로 보지 않게 한다."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as r:
            return json.loads(r.read()) == {"status": "ok"}
    except (OSError, ValueError):
        return False


def _compose_up() -> bool:
    """패키지의 docker-compose.yml로 Redis를 띄운다. 프로젝트 이름을 정해 다른 compose와 섞이지
    않게 한다."""
    compose = files.PACKAGE_ROOT / "docker-compose.yml"
    return _quiet(["docker", "compose", "-p", "pr-proof", "-f", str(compose), "up", "-d"], 300)


def _checks(redis_url: str) -> list[preflight.Check]:
    return preflight.run_checks(Redis.from_url(redis_url, socket_connect_timeout=2))


def _claude_ok() -> bool:
    return preflight.claude_login_ok(preflight._claude_auth_status(), datetime.now(UTC))


def real_deps(environ: Mapping[str, str]) -> Deps:
    hints = json.loads((Path(__file__).with_name("install_hints.json")).read_text())
    return Deps(
        which=lambda name: shutil.which(name) is not None,
        docker_ok=lambda: _quiet(["docker", "info"]),
        runner=gh.make_runner,
        claude_ok=_claude_ok,
        redis_ok=_redis_ok,
        compose_up=_compose_up,
        port_busy=_port_busy,
        pr_proof_on=_pr_proof_on,
        # 입구(bin/pr-proof.js)가 자기 node를 PR_PROOF_NODE로 넘긴다
        smee_new=lambda: gh.new_smee_url(environ.get("PR_PROOF_NODE") or shutil.which("node")),
        checks=_checks,
        environ=environ,
        hints=hints,
    )


# --- 공통 ---


def _ask_until(io: Io, prompt: str, valid: Callable[[str], str | None]) -> str:
    while True:
        answer = io.ask(prompt).strip()
        problem = valid(answer)
        if problem is None:
            return answer
        io.say(problem)


def _repos_problem(repos: Sequence[str]) -> str | None:
    if not repos:
        return f"repo를 하나 이상 적습니다 (예: {pure.REPO_EXAMPLE})"
    for repo in repos:
        if problem := pure.repo_problem(repo):
            return problem
    return None


def _config_text(directory: Path) -> str:
    path = directory / files.CONFIG_FILE
    return path.read_text() if path.is_file() else ""


def _write_config(directory: Path, text: str) -> None:
    path = directory / files.CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(text)


def _need_configured(directory: Path, io: Io) -> None:
    if (
        not (directory / files.CONFIG_FILE).is_file()
        or not (directory / config.WEBHOOK_SECRET).is_file()
    ):
        io.say(_need_configure())
        raise Stop


def _saved_url(directory: Path, io: Io) -> str | None:
    try:
        return files.read_private(files.WEBHOOK_URL_FILE, directory)
    except config.ConfigError as e:
        io.say(str(e))
        raise Stop from e


def _secret(directory: Path) -> str | None:
    try:
        return config.read_secret(config.WEBHOOK_SECRET, directory)
    except config.ConfigError:
        return None


def check_access(run: gh.Runner, repos: Sequence[str], io: Io) -> list[str]:
    """읽을 수 있는 repo만, GitHub이 적는 정식 이름으로 돌려준다(대소문자가 사용자가 적은 것과 다를
    수 있다. 웹훅 소식에는 정식 이름이 온다). 못 읽는 것과 쓸 수 없는 것을 말한다."""
    readable = []
    for given in repos:
        repo = given
        try:
            read, write, repo = gh.repo_access(run, given)
        except gh.NoPermission:
            io.say(f"  {repo}: 찾지 못했거나 읽을 권한이 없습니다")
            continue
        except gh.SetupGitHubError as e:
            io.say(f"  {repo}: 권한을 확인하지 못했습니다: {e}")
            continue
        if not read:
            io.say(f"  {repo}: 읽을 권한이 없습니다")
            continue
        readable.append(repo)
        if repo != given:
            io.say(f"  {given} → {repo} (GitHub에 적힌 이름으로 넣습니다)")
        if write:
            io.say(f"  {repo}: 읽기·쓰기 권한 있음")
        else:
            io.say(f"  {repo}: 쓰기 권한이 없습니다. 결과 코멘트와 수정안 PR을 올리지 못합니다")
    return readable


def ensure_hooks(
    run: gh.Runner, repos: Sequence[str], url: str, directory: Path, io: Io, opts: Options
) -> bool:
    """repo마다 그 주소의 웹훅을 만들거나 맞춘다. 권한이 없으면 손으로 등록할 값을 보인다.
    모두 등록됐으면 참."""
    secret_path = str(directory / config.WEBHOOK_SECRET)
    secret = _secret(directory) or "(dry-run: 아직 만들지 않은 비밀값)"
    all_ok = True
    for repo in repos:
        if opts.manual_webhook:
            io.say(pure.manual_hook_text(repo, url, secret_path))
            all_ok = False
            continue
        try:
            plan = pure.plan_hook(gh.list_hooks(run, repo), url)
        except gh.NoPermission:
            io.say(
                f"  {repo}: 웹훅을 볼 권한이 없습니다 (저장소 관리자 권한, 토큰이면 Webhooks 권한)"
            )
            io.say(pure.manual_hook_text(repo, url, secret_path))
            all_ok = False
            continue
        if plan.action == "conflict":
            others = ", ".join(plan.others)
            io.say(f"  {repo}: pr-proof 웹훅으로 보이는 다른 주소가 있어 만들지 않습니다: {others}")
            io.say("    같은 소식이 두 번 오지 않게 하려면 그 주소를 --webhook-url로 주어 다시 "
                   "실행하거나, 그 웹훅을 지운 뒤 다시 실행하세요.")  # fmt: skip
            all_ok = False
            continue
        what = "만든다" if plan.action == "create" else "맞춘다"
        if opts.dry_run:
            io.say(f"  (dry-run) {repo}: 웹훅을 {what}: {'; '.join(plan.changes)}")
            continue
        payload = pure.hook_payload(url, secret, create=plan.action == "create")
        try:
            if plan.action == "create":
                gh.create_hook(run, repo, payload)
            else:
                assert plan.hook_id is not None
                gh.update_hook(run, repo, plan.hook_id, payload)
        except gh.NoPermission:
            io.say(f"  {repo}: 웹훅을 등록할 권한이 없습니다")
            io.say(pure.manual_hook_text(repo, url, secret_path))
            all_ok = False
            continue
        io.say(f"  {repo}: 웹훅을 {'만들었습니다' if plan.action == 'create' else '맞췄습니다'}")
    return all_ok


def add_to_config(directory: Path, repos: Sequence[str], io: Io, dry_run: bool) -> list[str]:
    text = _config_text(directory) or CONFIG_HEADER
    try:
        new_text, added = pure.add_repos(text, repos)
    except pure.ConfigEditError as e:
        io.say(str(e))
        raise Stop from e
    for repo in repos:
        if repo not in added:
            io.say(f"  {repo}: 이미 허용 목록에 있음")
        else:
            io.say(
                f"  (dry-run) {repo}: 허용 목록에 더한다"
                if dry_run
                else f"  {repo}: 허용 목록에 더함"
            )
    if not dry_run and (added or not (directory / files.CONFIG_FILE).is_file()):
        _write_config(directory, new_text)
    return added


# --- configure ---


def _need_configure() -> str:
    return f"설정이 없습니다. 먼저 {files.command('configure')}를 실행하세요."


def _step(io: Io, number: int, title: str) -> None:
    io.say(f"\n[{number}/{STEPS}] {title}")


def _programs(io: Io, deps: Deps) -> None:
    _step(io, 1, "프로그램 확인")
    missing = [p for p in PROGRAMS if not deps.which(p)]
    if missing:
        io.say(pure.missing_programs_text(missing, deps.hints, deps.platform))
        raise Stop
    io.say("  docker, git, gh, claude 있음")
    _step(io, 2, "Docker")
    if not deps.docker_ok():
        io.say("  Docker가 꺼져 있습니다. Docker Desktop(Linux는 docker 서비스)을 실행한 뒤 다시 "
               "실행하세요.")  # fmt: skip
        raise Stop
    io.say("  켜져 있음")


def _install(directory: Path, io: Io, deps: Deps, opts: Options) -> str:
    _step(io, 3, "설치 (포트, Redis)")
    saved = files.saved_port(directory)
    port = opts.port or saved or files.DEFAULT_PORT
    # 이 설정이 이미 쓰던 포트에서 pr-proof가 돌고 있으면 다시 돌린 configure의 자기 서버다. 저장한
    # 적 없는 포트에서 도는 pr-proof는 다른 설정의 서버라 겹친다
    while deps.port_busy(port) and not (port == saved and deps.pr_proof_on(port)):
        who = "다른 pr-proof 서버가" if deps.pr_proof_on(port) else "다른 프로그램이"
        if not io.interactive:
            io.say(f"  포트 {port}를 {who} 쓰고 있습니다. --port로 다른 번호를 주세요.")
            raise Stop
        answer = _ask_until(io, f"  포트 {port}를 {who} 쓰고 있습니다. 쓸 번호: ",
                            lambda a: None if a.isdigit() and 0 < int(a) < 65536
                            else "1에서 65535 사이의 번호를 적습니다")  # fmt: skip
        port = int(answer)
    if opts.dry_run:
        io.say(f"  (dry-run) API 포트 {port}를 {directory / files.RUN_ENV_FILE}에 적는다")
    else:
        files.write_port(port, directory)
        io.say(f"  API 포트: {port}")
    try:
        data = {"repos": [], **pure.config_data(_config_text(directory))}
        redis_url = config.PipelineConfig.model_validate(data).redis_url
    except (pure.ConfigEditError, ValueError) as e:
        io.say(f"  config.yaml을 읽지 못했습니다: {e}")
        raise Stop from e
    if deps.redis_ok(redis_url):
        io.say(f"  Redis: {redis_url}에 연결됨")
    elif redis_url != config.PipelineConfig(repos=[]).redis_url:
        io.say(f"  Redis({redis_url})에 연결하지 못했습니다. config.yaml의 redis_url을 확인하세요.")
        raise Stop
    elif opts.dry_run:
        io.say("  (dry-run) Redis를 docker compose로 띄운다")
    elif deps.compose_up() and deps.redis_ok(redis_url):
        io.say("  Redis를 띄웠습니다 (docker compose 프로젝트 pr-proof)")
    else:
        io.say("  Redis를 띄우지 못했습니다. 6379 포트를 다른 프로그램이 쓰고 있는지 확인하세요.")
        raise Stop
    return redis_url


def _choose_auth(directory: Path, io: Io, deps: Deps, opts: Options) -> str:
    if opts.auth:
        return opts.auth
    if github_auth.uses_token(directory):
        return "token"
    if not io.interactive:
        if gh.token_from_env(deps.environ):
            return "token"
        if gh.auth_ok(deps.runner(None), token_mode=False):
            return "gh"
        io.say("  GitHub 인증 방식이 없습니다: --auth gh (gh auth login을 먼저) 또는 --auth token "
               f"(토큰은 환경 변수 {gh.TOKEN_INPUT_ENV}로)")  # fmt: skip
        raise Stop
    default = "1" if gh.auth_ok(deps.runner(None), token_mode=False) else "2"
    answer = _ask_until(
        io, f"  인증 방식 1) gh 로그인  2) 토큰  [기본 {default}]: ",
        lambda a: None if a in ("", "1", "2") else "1이나 2를 적습니다",
    )  # fmt: skip
    return "gh" if (answer or default) == "1" else "token"


def _auth(directory: Path, io: Io, deps: Deps, opts: Options) -> gh.Runner:
    _step(io, 4, "GitHub 인증")
    method = _choose_auth(directory, io, deps, opts)
    token_path = directory / github_auth.TOKEN_FILE
    if method == "gh":
        if token_path.exists():
            io.say(f"  {token_path}가 있어 토큰 방식이 앞섭니다.")
            io.say("  gh 로그인을 쓰려면 그 파일을 지운 뒤 다시 실행하세요.")
            raise Stop
        run = deps.runner(None)
        while not gh.auth_ok(run, token_mode=False):
            io.say("  gh 로그인이 되어 있지 않습니다. 다른 터미널에서 gh auth login을 실행하세요.")
            if not io.interactive or io.ask("  한 뒤 Enter (그만두려면 q): ").strip() == "q":
                raise Stop
        io.say(f"  gh 로그인: {gh.login(run)}")
        return run
    # 토큰 방식. 있는 토큰 파일은 덮어쓰지 않는다(확인이 실패해 사용자가 새로 넣을 때만 바꾼다)
    try:
        saved = github_auth.token(directory)
    except config.ConfigError as e:
        io.say(f"  {e}")
        raise Stop from e
    candidate = saved or gh.token_from_env(deps.environ)
    if candidate is None and io.interactive:
        io.say(pure.TOKEN_PERMISSIONS)
    while True:
        if candidate is None:
            if not io.interactive:
                io.say(f"  토큰이 없습니다. 환경 변수 {gh.TOKEN_INPUT_ENV}로 주세요.")
                io.say("  (셸 기록에 남지 않게 명령줄 인자로는 받지 않습니다)")
                raise Stop
            candidate = io.ask_secret("  GitHub 토큰 (화면에 보이지 않습니다. 그만두려면 Enter): ")
            candidate = candidate.strip() or None
            if candidate is None:
                raise Stop
        run = deps.runner(candidate)
        if gh.auth_ok(run, token_mode=True):
            break
        io.say("  토큰으로 GitHub에 로그인하지 못했습니다 (틀렸거나 만료·폐기됐습니다).")
        if not io.interactive:
            raise Stop
        io.say(pure.TOKEN_PERMISSIONS)
        candidate = None
    if candidate != saved:
        if opts.dry_run:
            io.say(f"  (dry-run) 토큰을 {token_path}에 쓴다 (600)")
        else:
            files.write_private(token_path, candidate, replace=True)
            io.say(f"  토큰을 {token_path}에 저장했습니다 (600)")
    io.say(f"  토큰 계정: {gh.login(run)}")
    return run


def _claude(io: Io, deps: Deps) -> None:
    _step(io, 5, "Claude 로그인")
    while not deps.claude_ok():
        io.say("  claude CLI에 로그인되어 있지 않거나 로그인이 만료됐습니다. 모델 호출은 이 계정의 "
               "사용량으로 나갑니다.")  # fmt: skip
        io.say("  다른 터미널에서 claude를 실행해 /login 하세요.")
        if not io.interactive:
            io.say(f"  로그인한 뒤 {files.command('configure')}를 다시 실행하세요.")
            raise Stop
        answer = io.ask("  로그인한 뒤 Enter를 누르면 다시 확인합니다 (그만두려면 q, 나중에 "
                        f"{files.command('configure')}를 다시 실행해도 됩니다): ")  # fmt: skip
        if answer.strip() == "q":
            raise Stop
    io.say("  로그인되어 있음")


def _model_label(name: str | None) -> str:
    return name if name is not None else f"기본값 ({pure.default_models_label()})"


def current_model(directory: Path, io: Io) -> str | None:
    """config.yaml의 `model.default`. 읽지 못하면 이유를 말하고 Stop."""
    try:
        return pure.config_default_model(_config_text(directory))
    except pure.ConfigEditError as e:
        io.say(f"  {e}")
        raise Stop from e


def save_model(directory: Path, name: str | None, io: Io, dry_run: bool) -> None:
    """config.yaml의 `model.default`를 name으로 적는다(None이면 지운다). 다른 설정과 주석은 둔다.
    파일이 없으면 configure가 만드는 머리말과 함께 만든다."""
    text = _config_text(directory) or CONFIG_HEADER
    try:
        new_text = pure.set_default_model(text, name)
    except pure.ConfigEditError as e:
        io.say(f"  {e}")
        raise Stop from e
    path = directory / files.CONFIG_FILE
    if dry_run:
        what = f"model.default를 {name}로 적는다" if name else "model.default를 지운다"
        io.say(f"  (dry-run) {path}의 {what}")
    else:
        _write_config(directory, new_text)


def _ask_model(io: Io, current: str | None) -> str | None:
    """터미널에서 고른 모델. Enter면 지금 값 그대로다."""
    io.say("  모든 단계에 쓸 모델을 고릅니다. 저장소의 .pipeline.yaml에 단계별로 적은 값이 "
           "앞섭니다.")  # fmt: skip
    io.say(f"    1) 기본값 ({pure.default_models_label()})")
    for number, name in enumerate(MODEL_CHOICES, start=2):
        io.say(f"    {number}) {name}")
    typed = len(MODEL_CHOICES) + 2
    io.say(f"    {typed}) 직접 입력 (claude --model이 받는 이름)")
    io.say(f"  지금: {_model_label(current)}")
    answer = _ask_until(
        io, "  고르기 [Enter: 그대로]: ",
        lambda a: None if a == "" or (a.isdigit() and 1 <= int(a) <= typed)
        else f"1에서 {typed} 사이의 번호를 적습니다",
    )  # fmt: skip
    if answer == "":
        return current
    if answer == "1":
        return None
    if int(answer) == typed:
        return _ask_until(io, "  모델 이름: ", pure.model_name_problem)
    return MODEL_CHOICES[int(answer) - 2]


def _model_step(directory: Path, io: Io, opts: Options) -> None:
    _step(io, 6, "모델")
    current = current_model(directory, io)
    if opts.model is not None:
        wanted = None if opts.model == pure.DEFAULT_MODEL_WORD else opts.model
    elif io.interactive:
        wanted = _ask_model(io, current)
    else:
        io.say(f"  모델: {_model_label(current)} (바꾸려면 --model <이름>)")
        return
    if wanted == current:
        io.say(f"  모델: {_model_label(current)} (그대로)")
        return
    save_model(directory, wanted, io, opts.dry_run)
    if not opts.dry_run:
        io.say(f"  모델: {_model_label(wanted)}")
    io.say(f"  {SAVED_JUDGMENTS}")


def _repos(directory: Path, io: Io, run: gh.Runner, opts: Options) -> list[str]:
    _step(io, 7, "repo 등록과 권한 확인")
    repos = list(dict.fromkeys(pure.repo_name(r) for r in opts.repos))
    try:
        existing = pure.config_repos(_config_text(directory))
    except pure.ConfigEditError as e:
        io.say(f"  {e}")
        raise Stop from e
    if not repos and not existing:
        if not io.interactive:
            io.say(f"  처리할 repo가 없습니다. --repo로 주세요 (예: --repo {pure.REPO_EXAMPLE})")
            raise Stop
        answer = _ask_until(
            io, f"  처리할 repo (예: {pure.REPO_EXAMPLE}, 여럿이면 띄어 쓴다): ",
            lambda a: _repos_problem(a.split()),
        )  # fmt: skip
        repos = list(dict.fromkeys(pure.repo_name(r) for r in answer.split()))
    if problem := _repos_problem(repos or existing):
        io.say(f"  {problem}")
        raise Stop
    readable = check_access(run, repos, io)
    if len(readable) < len(repos):
        io.say("  읽을 수 없는 repo는 허용 목록에 더하지 않았습니다. 이름과 권한을 확인하세요.")
        raise Stop
    repos = readable  # 정식 이름
    add_to_config(directory, repos, io, opts.dry_run)
    others = [r for r in existing if pure.spelled_as(r, repos) is None]
    if others:
        check_access(run, others, io)
    # 목록에 대소문자만 다르게 적혀 있던 것은 그 철자를 둔다(서버는 대소문자를 가리지 않고 맞춘다)
    return list(
        dict.fromkeys(existing + [r for r in repos if pure.spelled_as(r, existing) is None])
    )


def _webhook_url(directory: Path, io: Io, deps: Deps, opts: Options) -> str:
    saved = _saved_url(directory, io)
    path = directory / files.WEBHOOK_URL_FILE
    url = opts.webhook_url
    if url is None and saved:
        io.say(f"  웹훅 받는 주소: {saved} (저장된 것)")
        return saved
    if url is None and io.interactive:
        io.say("  웹훅 받는 주소: GitHub이 PR 소식을 보낼, pr-proof가 도는 이 컴퓨터로 들어오는")
        io.say(
            "  주소다. repo가 공개인지와 상관없다. 있으면 적고(예: https://example.com/webhook/github),"
        )
        io.say("  없으면 Enter를 누르면 smee.io 주소를 만들어 쓴다.")
        url = io.ask("  웹훅 받는 주소 [Enter: smee]: ").strip() or None
    if url is None:
        io.say("  PR 소식이 smee.io를 거쳐 이 컴퓨터로 옵니다.")
        io.say("  (smee 주소를 아는 사람은 그 소식을 볼 수 있습니다. 주소는 600 파일에 둡니다)")
        if opts.dry_run:
            io.say(f"  (dry-run) https://smee.io/new로 주소를 만들어 {path}에 쓴다 (600)")
            return "https://smee.io/(dry-run)"
        try:
            url = deps.smee_new()
        except gh.SetupGitHubError as e:
            io.say(f"  {e}")
            raise Stop from e
    elif not url.startswith("https://"):
        io.say("  웹훅 받는 주소는 https://로 시작합니다.")
        raise Stop
    if opts.dry_run:
        io.say(f"  (dry-run) 웹훅 받는 주소 {url}를 {path}에 쓴다 (600)")
    else:
        files.write_private(path, url, replace=True)
        io.say(f"  웹훅 받는 주소: {url} ({path}, 600)")
    return url


def _webhook(directory: Path, io: Io, deps: Deps, run: gh.Runner, repos: Sequence[str],
             opts: Options) -> bool:  # fmt: skip
    _step(io, 8, "웹훅")
    secret_path = directory / config.WEBHOOK_SECRET
    if secret_path.exists():
        io.say(f"  비밀값: {secret_path} (있는 것을 씁니다)")
    elif opts.dry_run:
        io.say(f"  (dry-run) 비밀값을 만들어 {secret_path}에 쓴다 (600)")
    else:
        files.write_private(secret_path, deps.new_secret())
        io.say(f"  비밀값을 만들었습니다: {secret_path} (600)")
    url = _webhook_url(directory, io, deps, opts)
    return ensure_hooks(run, repos, url, directory, io, opts)


def _final(io: Io, deps: Deps, redis_url: str) -> bool:
    _step(io, 9, "끝 확인 (서버가 시작할 때 하는 확인과 같다)")
    checks = deps.checks(redis_url)
    io.say(pure.check_table([(c.name, c.ok, c.hint) for c in checks]))
    return all(c.ok for c in checks)


def configure(opts: Options, io: Io, deps: Deps, directory: Path | None = None) -> int:
    directory = directory or files.config_dir()
    if problem := (opts.repos and _repos_problem(opts.repos)):
        io.say(problem)
        return 1
    if opts.model not in (None, pure.DEFAULT_MODEL_WORD) and (
        problem := pure.model_name_problem(opts.model)
    ):
        io.say(problem)
        return 1
    if opts.dry_run:
        io.say("dry-run: 파일과 GitHub에 쓰지 않고 할 일만 보입니다.")
    try:
        _programs(io, deps)
        redis_url = _install(directory, io, deps, opts)
        run = _auth(directory, io, deps, opts)
        _claude(io, deps)
        _model_step(directory, io, opts)
        repos = _repos(directory, io, run, opts)
        hooks_ok = _webhook(directory, io, deps, run, repos, opts)
    except Stop:
        return 1
    checks_ok = _final(io, deps, redis_url)
    io.say("")
    if opts.manual_webhook:
        io.say("웹훅은 위의 값으로 손으로 등록하세요.")
    elif not hooks_ok:
        io.say("웹훅을 모두 등록하지는 못했습니다. 위의 값으로 손으로 등록하세요.")
    if not checks_ok:
        io.say(f"끝 확인에 실패한 줄이 있습니다. 고친 뒤 {files.command('configure')}를 다시 "
               "실행하세요.")  # fmt: skip
    io.say(f"다음: {files.command('start')}")
    io.say(f"계속 띄워 두려면: {OPERATIONS_URL}")
    # 손으로 등록하라고 한 것(--manual-webhook)은 실패가 아니다
    return 0 if checks_ok and (hooks_ok or opts.manual_webhook) else 1


# --- add / rm / list ---


def add(opts: Options, io: Io, deps: Deps, directory: Path | None = None) -> int:
    directory = directory or files.config_dir()
    if problem := _repos_problem(opts.repos):
        io.say(problem)
        return 1
    try:
        _need_configured(directory, io)
        saved = _saved_url(directory, io)
        url = opts.webhook_url or saved
        if url is None:
            io.say("웹훅 받는 주소가 저장되어 있지 않습니다. --webhook-url로 주세요.")
            return 1
        run = deps.runner(None)
        if not gh.auth_ok(run, token_mode=github_auth.uses_token(directory)):
            io.say(f"GitHub 인증이 되지 않습니다 → {github_auth.hint(directory)}")
            return 1
        wanted = list(dict.fromkeys(pure.repo_name(r) for r in opts.repos))
        readable = check_access(run, wanted, io)
        if len(readable) < len(wanted):
            io.say("읽을 수 없는 repo가 있어 아무것도 더하지 않았습니다.")
            return 1
        added = add_to_config(directory, readable, io, opts.dry_run)
        if opts.webhook_url and opts.webhook_url != saved and not opts.dry_run:
            files.write_private(directory / files.WEBHOOK_URL_FILE, opts.webhook_url, replace=True)
        hooks_ok = ensure_hooks(run, readable, url, directory, io, opts)
    except Stop:
        return 1
    if added and not opts.dry_run:
        io.say(RESTART)
    return 0 if hooks_ok or opts.manual_webhook else 1


def remove(opts: Options, io: Io, deps: Deps, directory: Path | None = None) -> int:
    directory = directory or files.config_dir()
    if problem := _repos_problem(opts.repos):
        io.say(problem)
        return 1
    path = directory / files.CONFIG_FILE
    if not path.is_file():
        io.say(_need_configure())
        return 1
    text = path.read_text()
    current = pure.config_repos(text)
    names = list(dict.fromkeys(pure.repo_name(r) for r in opts.repos))
    targets = list(dict.fromkeys(s for r in names if (s := pure.spelled_as(r, current))))
    for repo in names:
        if pure.spelled_as(repo, current) is None:
            io.say(f"  {repo}: 허용 목록에 없습니다. 아무것도 하지 않습니다.")
    if not targets:
        return 0
    try:
        url = _saved_url(directory, io)
    except Stop:
        return 1
    io.say(f"뺄 repo: {', '.join(targets)}")
    if url:
        io.say(f"  허용 목록에서 빼고, 그 repo의 {url} 웹훅을 지웁니다.")
    else:
        io.say("  허용 목록에서 뺍니다. 웹훅 받는 주소가 저장되어 있지 않아 웹훅은 그대로 둡니다.")
    io.say(f"  {LEFT_ALONE}")
    if not opts.yes and not opts.dry_run:
        if not io.interactive:
            io.say("터미널이 아니라 확인을 물을 수 없습니다. 지우려면 --yes를 주세요.")
            return 1
        if io.ask("계속할까요? [y/N]: ").strip().lower() not in ("y", "yes"):
            io.say("그만둡니다.")
            return 1
    try:
        new_text, removed = pure.remove_repos(text, targets)
    except pure.ConfigEditError as e:
        io.say(str(e))
        return 1
    if opts.dry_run:
        io.say(f"  (dry-run) 허용 목록에서 뺀다: {', '.join(removed)}")
    else:
        path.write_text(new_text)
        io.say(f"  허용 목록에서 뺐습니다: {', '.join(removed)}")
    all_ok = True
    if url is not None:
        run = deps.runner(None)
        for repo in removed:
            all_ok &= _remove_hooks(run, repo, url, io, opts.dry_run)
    if not opts.dry_run:
        io.say(RESTART)
    return 0 if all_ok else 1


def _remove_hooks(run: gh.Runner, repo: str, url: str, io: Io, dry_run: bool) -> bool:
    manual = f"  {repo}: 웹훅은 손으로 지우세요 (GitHub 설정 → Webhooks, 주소 {url})"
    try:
        hooks = pure.hooks_at(gh.list_hooks(run, repo), url)
    except gh.SetupGitHubError:
        io.say(manual)
        return False
    if not hooks:
        io.say(f"  {repo}: 지울 웹훅이 없습니다")
        return True
    for hook in hooks:
        if dry_run:
            io.say(f"  (dry-run) {repo}: 웹훅 {hook.id}를 지운다")
            continue
        try:
            gh.delete_hook(run, repo, hook.id)
        except gh.SetupGitHubError:
            io.say(manual)
            return False
        io.say(f"  {repo}: 웹훅을 지웠습니다")
    return True


def list_repos(opts: Options, io: Io, deps: Deps, directory: Path | None = None) -> int:
    directory = directory or files.config_dir()
    path = directory / files.CONFIG_FILE
    if not path.is_file():
        io.say(_need_configure())
        return 1
    try:
        repos = pure.config_repos(path.read_text())
    except pure.ConfigEditError as e:
        io.say(str(e))
        return 1
    if not repos:
        io.say(f"처리하는 repo가 없습니다. 더하려면: {files.command('add <repo>')}")
        return 0
    if not opts.check:
        for repo in repos:
            io.say(repo)
        return 0
    try:
        url = files.read_private(files.WEBHOOK_URL_FILE, directory)
    except config.ConfigError:
        url = None
    run = deps.runner(None)
    for repo in repos:
        try:
            status = pure.hook_status(gh.list_hooks(run, repo), url)
        except gh.NoPermission:
            status = "웹훅을 확인하지 못함 (권한 없음)"
        except gh.SetupGitHubError:
            status = "웹훅을 확인하지 못함 (GitHub 조회 실패)"
        io.say(f"{repo}  {status}")
    return 0


# --- model ---


def model(name: str | None, io: Io, directory: Path | None = None) -> int:
    """`model`: 보기. `model <이름>`: 모든 단계를 그 모델로. `model default`: 기본값으로.

    이름은 꼴만 본다. 그 모델이 실제로 있는지는 모델을 불러야 알 수 있어(사용량이 든다) 보지
    않는다. 틀린 이름이면 첫 검사의 모델 호출이 실패하고 결과 코멘트에 그 단계가 실패했다고 나온다.
    """
    directory = directory or files.config_dir()
    if not (directory / files.CONFIG_FILE).is_file():
        io.say(_need_configure())
        return 1
    try:
        current = current_model(directory, io)
    except Stop:
        return 1
    if name is None:
        io.say(pure.model_view(current, files.command_prefix()))
        if current is not None and (problem := pure.model_name_problem(current)):
            io.say(f"\nconfig.yaml의 model.default가 틀려 서버가 시작하지 못합니다. {problem}")
            return 1
        return 0
    wanted = None if name == pure.DEFAULT_MODEL_WORD else name
    if wanted is not None and (problem := pure.model_name_problem(wanted)):
        io.say(problem)
        return 1
    if wanted == current:
        io.say(f"이미 {_model_label(current)}입니다. 바꾸지 않았습니다.")
        return 0
    try:
        save_model(directory, wanted, io, dry_run=False)
    except Stop:
        return 1
    if wanted is None:
        io.say(f"파이프라인 쪽 모델을 지워 단계별 기본값({pure.default_models_label()})으로 "
               "되돌렸습니다.")  # fmt: skip
    else:
        io.say(f"모든 단계의 모델을 {wanted}로 정했습니다 (config.yaml의 model.default). "
               "저장소의 .pipeline.yaml에 단계별로 적은 값은 이 값보다 앞섭니다.")  # fmt: skip
    io.say(SAVED_JUDGMENTS)
    io.say(RESTART)
    return 0
