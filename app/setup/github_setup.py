"""configure·add·rm·list가 부르는 GitHub (gh CLI)와 smee.io.

GitHub 호출은 받는 runner(`Runner`)로 한다. 테스트는 runner를 바꿔 끼운다. 실제 runner는 서버와 같이
최소 환경으로 gh를 부르고, 토큰은 gh에만 넘긴다(`app/shared/github_auth.py`). configure가 토큰을
파일에 쓰기 전에 확인할 때는 그 토큰을 직접 받는다.
"""

import json
import os
import subprocess
from collections.abc import Callable, Mapping
from typing import Final

from app.setup.setup_pure import Hook
from app.shared import config, github_auth

GH_TIMEOUT: Final = 60
_ENV_PASSTHROUGH: Final = ("PATH", "HOME", "GH_CONFIG_DIR")
# 권한이 없거나 저장소가 보이지 않을 때 gh가 내는 것
_DENIED: Final = ("HTTP 403", "HTTP 404", "Resource not accessible", "Must have admin rights")
SMEE_HOST: Final = "smee.io"
TOKEN_INPUT_ENV: Final = "PR_PROOF_GITHUB_TOKEN"

Runner = Callable[[list[str], str | None], str]


class SetupGitHubError(RuntimeError):
    """gh 호출 실패."""


class NoPermission(SetupGitHubError):
    """권한이 없거나 저장소가 보이지 않는다 (403, 404)."""


def make_runner(token: str | None = None) -> Runner:
    """gh를 부르는 runner. token을 주면 그 토큰으로, 아니면 저장한 인증(토큰 파일 또는 gh
    로그인)으로."""

    def run(args: list[str], stdin: str | None = None) -> str:
        try:
            env = github_auth.gh_env(_ENV_PASSTHROUGH)
        except config.ConfigError as e:
            raise SetupGitHubError(f"GitHub 토큰을 읽지 못함: {e}") from e
        if token is not None:
            env[github_auth.TOKEN_ENV] = token
        try:
            proc = subprocess.run(
                ["gh", *args], input=stdin, capture_output=True, text=True, env=env,
                timeout=GH_TIMEOUT, check=False,
            )  # fmt: skip
        except (OSError, subprocess.TimeoutExpired) as e:
            raise SetupGitHubError(f"gh 실행 실패: {e}") from e
        if proc.returncode != 0:
            detail = github_auth.redact(proc.stderr.strip(), env)[-500:]
            error = NoPermission if any(d in detail for d in _DENIED) else SetupGitHubError
            raise error(f"gh {' '.join(args[:3])} 실패: {detail}")
        return proc.stdout

    return run


def auth_ok(run: Runner, token_mode: bool) -> bool:
    """고른 인증이 되는지. 토큰 방식은 그 토큰 계정만 본다."""
    try:
        run(["auth", "status", *(["--active"] if token_mode else [])], None)
    except SetupGitHubError:
        return False
    return True


def login(run: Runner) -> str:
    return run(["api", "user", "--jq", ".login"], None).strip()


def repo_access(run: Runner, repo: str) -> tuple[bool, bool]:
    """(읽기, 쓰기). 저장소가 보이지 않으면 NoPermission.

    GitHub이 주는 것은 계정의 저장소 권한(`permissions.push`)이다. fine-grained 토큰에서 그 토큰에
    준 권한이 모자란지는 실제로 쓰기 전에는 알 수 없다(README의 권한 목록을 본다).
    """
    out = run(["api", f"repos/{repo}", "--jq", "[.permissions.pull, .permissions.push] | @tsv"],
              None)  # fmt: skip
    pull, _, push = out.strip().partition("\t")
    return pull == "true", push == "true"


def list_hooks(run: Runner, repo: str) -> list[Hook]:
    jq = (
        '.[] | {id, active, events, url: (.config.url // ""), '
        'content_type: (.config.content_type // "")} | @json'
    )
    out = run(["api", "--paginate", f"repos/{repo}/hooks", "--jq", jq], None)
    hooks = []
    for line in out.splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        hooks.append(Hook(
            id=int(item["id"]), url=str(item["url"]), events=tuple(item.get("events") or ()),
            active=bool(item.get("active")), content_type=str(item.get("content_type") or ""),
        ))  # fmt: skip
    return hooks


def create_hook(run: Runner, repo: str, payload: dict[str, object]) -> int:
    out = run(["api", "-X", "POST", f"repos/{repo}/hooks", "--input", "-", "--jq", ".id"],
              json.dumps(payload))  # fmt: skip
    return int(out.strip())


def update_hook(run: Runner, repo: str, hook_id: int, payload: dict[str, object]) -> None:
    run(["api", "-X", "PATCH", f"repos/{repo}/hooks/{hook_id}", "--input", "-", "--jq", ".id"],
        json.dumps(payload))  # fmt: skip


def delete_hook(run: Runner, repo: str, hook_id: int) -> None:
    run(["api", "-X", "DELETE", f"repos/{repo}/hooks/{hook_id}"], None)


# `https://smee.io/new`가 새 주소로 넘겨 주는 것(307의 Location)을 읽는다(smee-client의
# createChannel과 같다). node로 한다: python.org에서 받은 macOS의 Python은 인증서를 따로 깔기
# 전에는 HTTPS 확인이 실패한다. node는 입구의 전제이고 자기 인증서 묶음을 쓴다
_SMEE_NEW_JS: Final = (
    f"fetch('https://{SMEE_HOST}/new', {{method: 'HEAD', redirect: 'manual'}})"
    ".then((r) => console.log(r.headers.get('location') || ''))"
    ".catch((e) => { console.error(String(e)); process.exit(1); })"
)


def new_smee_url(node: str | None, timeout: float = 20) -> str:
    """smee.io에 새 채널을 만들어 그 주소를 돌려준다."""
    if not node:
        raise SetupGitHubError("smee 주소를 만들 node를 찾지 못함")
    env = {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}
    try:
        proc = subprocess.run([node, "-e", _SMEE_NEW_JS], capture_output=True, text=True,
                              env=env, timeout=timeout, check=False)  # fmt: skip
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SetupGitHubError(f"smee 주소를 만들지 못함: {e}") from e
    location = proc.stdout.strip()
    if proc.returncode != 0 or not location.startswith(f"https://{SMEE_HOST}/"):
        raise SetupGitHubError(f"smee 주소를 만들지 못함: {proc.stderr.strip()[-300:]}")
    return location


def token_from_env(environ: Mapping[str, str] | None = None) -> str | None:
    """질문 없이 돌 때 토큰을 받는 환경 변수. 명령줄 인자로는 받지 않는다(셸 기록에 남는다)."""
    source = os.environ if environ is None else environ
    return (source.get(TOKEN_INPUT_ENV) or "").strip() or None
