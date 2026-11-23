"""설정 폴더(`~/.config/pipeline`, `PIPELINE_CONFIG_DIR`)에 configure가 두는 파일과 실행 환경 폴더.

| 파일 | 내용 | 권한 |
| --- | --- | --- |
| `config.yaml` | 처리할 repo 목록과 설정 (서버가 읽는다) | |
| `webhook_secret` | 웹훅 비밀값 | 600 |
| `github_token` | 토큰 방식의 GitHub 토큰 (`app/shared/github_auth.py`) | 600 |
| `webhook_url` | 웹훅 받는 주소. smee 주소면 start가 smee-client를 띄운다 | 600 |
| `pr-proof.env` | `npx pr-proof start`의 실행 설정 (`API_PORT`). 비밀값은 넣지 않는다 | |

실행 환경 폴더(`PR_PROOF_HOME`, 기본 `~/.cache/pipeline/runtime`)에는 start가 만드는 Procfile과,
npm 패키지로 깔았을 때의 venv가 있다(bin/pr-proof.js와 같은 규칙).
"""

import os
import tempfile
from pathlib import Path
from typing import Final

from app.shared import config

PACKAGE_ROOT: Final = Path(__file__).resolve().parents[2]
CONFIG_FILE: Final = "config.yaml"
WEBHOOK_URL_FILE: Final = "webhook_url"
RUN_ENV_FILE: Final = "pr-proof.env"
PORT_KEY: Final = "API_PORT"
DEFAULT_PORT: Final = 8765
SMEE_PREFIX: Final = "https://smee.io/"
RUNTIME_HOME_ENV: Final = "PR_PROOF_HOME"


def config_dir() -> Path:
    return config.config_dir()


def runtime_home(environ: dict[str, str] | None = None) -> Path:
    source = os.environ if environ is None else environ
    default = Path.home() / ".cache" / "pipeline" / "runtime"
    return Path(source.get(RUNTIME_HOME_ENV) or default).expanduser()


def is_smee(url: str) -> bool:
    return url.startswith(SMEE_PREFIX)


def write_private(path: Path, text: str, *, replace: bool = False) -> bool:
    """600 파일로 쓴다. 이미 있으면 replace일 때만 바꾼다. 썼으면 참.

    임시 파일을 600으로 만들어 쓴 뒤 이름을 바꾼다(넓은 권한으로 잠깐이라도 보이지 않게).
    """
    if path.exists() and not replace:
        return False
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text.strip() + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True


def read_private(name: str, directory: Path | None = None) -> str | None:
    """설정 폴더의 600 파일. 없으면 None. 권한이 넓으면 `config.ConfigError`."""
    if not ((directory or config_dir()) / name).exists():
        return None
    return config.read_secret(name, directory)


def read_port(directory: Path | None = None) -> int:
    """`pr-proof.env`의 API_PORT. 없거나 읽지 못하면 기본값."""
    path = (directory or config_dir()) / RUN_ENV_FILE
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return DEFAULT_PORT
    for line in lines:
        key, _, value = line.partition("=")
        if key.strip() == PORT_KEY and value.strip().isdigit():
            return int(value.strip())
    return DEFAULT_PORT


def write_port(port: int, directory: Path | None = None) -> None:
    path = (directory or config_dir()) / RUN_ENV_FILE
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(
        "# npx pr-proof start가 honcho에 넘기는 실행 설정. 비밀값은 넣지 않는다.\n"
        f"{PORT_KEY}={port}\n"
    )
