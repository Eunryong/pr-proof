"""`npx pr-proof start`: api와 worker, 저장한 웹훅 받는 주소가 smee 주소면 smee-client까지 띄운다.

세 프로그램은 honcho 하나가 띄운다(`scripts/start.sh`와 같다). 하나가 끝나면 honcho가 나머지를 모두
내린다. launchd로 띄웠으면 launchd가 통째로 다시 띄운다(docs/OPERATIONS.md). smee-client는 연결이
끊기면 스스로 다시 붙고, 그사이 놓친 소식은 동기화가 메운다.

Procfile은 패키지의 것(api, worker)에 smee 줄을 더해 실행 환경 폴더에 만든다. 하위 프로세스에는
`scripts/start.sh`처럼 최소 환경만 넘긴다. node는 입구(bin/pr-proof.js)가 넘긴 그 node를 쓴다
(nvm으로 깐 node도 찾는다).
"""

import os
import shlex
import shutil
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final

from app.setup import files
from app.shared import config

# launchd는 /usr/bin:/bin만 준다. gh·claude·docker가 보통 있는 곳 (scripts/start.sh와 같다)
SYSTEM_PATH: Final = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
PASSED_ENV: Final = ("HOME", "USER", "TMPDIR", "LANG", config.CONFIG_DIR_ENV)
TOOLS: Final = ("gh", "claude", "docker", "git")
WEBHOOK_PATH: Final = "/webhook/github"

Exec = Callable[[str, list[str], Mapping[str, str]], object]


def procfile_text(node: str | None = None, smee_js: str | None = None,
                  smee_url: str | None = None) -> str:  # fmt: skip
    """패키지의 Procfile(api, worker)에 smee 줄을 더한 것. smee 주소가 없으면 그대로다."""
    text = (files.PACKAGE_ROOT / "Procfile").read_text()
    if not (node and smee_js and smee_url):
        return text
    target = f"http://127.0.0.1:${{{files.PORT_KEY}:-{files.DEFAULT_PORT}}}{WEBHOOK_PATH}"
    smee = (
        f"smee: {shlex.quote(node)} {shlex.quote(smee_js)} --url {shlex.quote(smee_url)} "
        f'--target "{target}"'
    )
    return text.rstrip("\n") + "\n" + smee + "\n"


def run_env(environ: Mapping[str, str], venv_bin: Path, node: str | None) -> dict[str, str]:
    """honcho와 그 아래 프로그램의 환경. PATH는 venv, node의 폴더, ~/.local/bin, 시스템 경로,
    이 셸의 PATH 순이다. 비밀값은 넣지 않는다(서버가 설정 폴더에서 직접 읽는다)."""
    home = environ.get("HOME", str(Path.home()))
    parts = [str(venv_bin)]
    if node:
        parts.append(str(Path(node).parent))
    parts += [f"{home}/.local/bin", *environ.get("PR_PROOF_SYSTEM_PATH", SYSTEM_PATH).split(":")]
    parts += environ.get("PATH", "").split(":")
    path = ":".join(dict.fromkeys(p for p in parts if p))
    env = {k: environ[k] for k in PASSED_ENV if environ.get(k)}
    return env | {"HOME": home, "PATH": path}


def missing_tools(path: str) -> list[str]:
    return [tool for tool in TOOLS if shutil.which(tool, path=path) is None]


def start(
    node: str | None,
    smee_js: str | None,
    *,
    directory: Path | None = None,
    environ: Mapping[str, str] | None = None,
    venv_bin: Path | None = None,
    exec_: Exec = os.execve,
    out: Callable[[str], object] = print,
) -> int:
    """honcho로 바꿔 탄다(exec). 돌아오면 띄우지 못한 것이다."""
    source = os.environ if environ is None else environ
    directory = directory or files.config_dir()
    bin_dir = venv_bin or Path(sys.executable).parent
    if not (directory / files.CONFIG_FILE).is_file():
        out(f"설정이 없습니다 ({directory / files.CONFIG_FILE}). 먼저 npx pr-proof configure를 "
            "실행하세요.")  # fmt: skip
        return 1
    honcho = bin_dir / "honcho"
    if not honcho.exists():
        out(f"{honcho}가 없습니다. npx pr-proof configure를 다시 실행하세요.")
        return 1
    env = run_env(source, bin_dir, node)
    missing = missing_tools(env["PATH"])
    if missing:
        out(f"찾지 못한 프로그램: {', '.join(missing)} (PATH={env['PATH']}). "
            "npx pr-proof configure로 확인하세요.")  # fmt: skip
        return 1
    try:
        url = files.read_private(files.WEBHOOK_URL_FILE, directory)
    except config.ConfigError as e:
        out(str(e))
        return 1
    smee_url = url if url and files.is_smee(url) else None
    if smee_url and not (node and smee_js):
        out("smee 주소가 저장되어 있지만 smee-client를 찾지 못했습니다. npx pr-proof start로 "
            "실행하세요.")  # fmt: skip
        return 1
    runtime = files.runtime_home(dict(source))
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    procfile = runtime / "Procfile"
    procfile.write_text(procfile_text(node, smee_js, smee_url))
    procfile.chmod(0o600)  # smee 주소가 들어 있다
    port = files.read_port(directory)
    out(f"pr-proof 시작: api(포트 {port}), worker" + (", smee에서 받아 오기" if smee_url else ""))
    os.chdir(files.PACKAGE_ROOT)  # api와 worker는 패키지 폴더에서 돈다 (scripts/start.sh와 같다)
    args = [str(honcho), "start", "-f", str(procfile), "-d", str(files.PACKAGE_ROOT),
            "-e", str(directory / files.RUN_ENV_FILE)]  # fmt: skip
    exec_(str(honcho), args, env)
    return 1
