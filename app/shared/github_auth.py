"""GitHub 인증 두 방식: gh 로그인, 또는 토큰 파일 (docs/design/08-security.md).

`<설정 폴더>/github_token`(chmod 600)이 있으면 토큰 방식이다. 없으면 `gh auth login`으로 저장한
로그인을 쓴다. 두 방식 모두 GitHub 호출은 `gh` 프로그램으로 한다.

토큰은 `gh`와 git(자격 증명 도우미로 `gh auth git-credential`을 부르는) 하위 프로세스의 환경에만
`GH_TOKEN`으로 넣는다. gh는 이 변수를 저장된 로그인보다 앞세운다(`gh help environment`). 이
프로세스의 `os.environ`에는 넣지 않으므로 모델 호출, 샌드박스, 도구 서버, 알림 같은 다른 하위
프로세스에는 가지 않는다.
"""

import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Final

from app.shared import config
from app.shared.ops_alert_pure import Kind

TOKEN_FILE: Final = "github_token"
TOKEN_ENV: Final = "GH_TOKEN"
REDACTED: Final = "<github_token>"


def uses_token(directory: Path | None = None) -> bool:
    """토큰 방식인지. 파일이 있기만 하면 참이다(권한·내용이 잘못됐으면 쓸 때 실패한다)."""
    return ((directory or config.config_dir()) / TOKEN_FILE).exists()


def token(directory: Path | None = None) -> str | None:
    """토큰 파일의 값. 파일이 없으면 None. 권한이 넓거나 비었으면 `config.ConfigError`."""
    if not uses_token(directory):
        return None
    return config.read_secret(TOKEN_FILE, directory)


def gh_env(
    names: Iterable[str],
    environ: Mapping[str, str] | None = None,
    directory: Path | None = None,
) -> dict[str, str]:
    """gh·git에 넘길 환경: 허용 목록의 변수와, 토큰 방식이면 `GH_TOKEN`."""
    source = os.environ if environ is None else environ
    env = {k: source[k] for k in names if k in source}
    value = token(directory)
    if value is not None:
        env[TOKEN_ENV] = value
    return env


def redact(text: str, env: Mapping[str, str]) -> str:
    """오류 메시지에 싣기 전에 토큰을 지운다. 자르기 전에 부른다(잘린 토큰 끝이 남지 않게)."""
    value = env.get(TOKEN_ENV)
    return text.replace(value, REDACTED) if value else text


def failure_detail(stderr: str, stdout: str, returncode: int, env: Mapping[str, str]) -> str:
    """gh가 실패했을 때 오류 글에 실을 까닭. stderr가 비면 stdout의 끝을 쓰고(gh api는 오류 응답을
    stdout에 찍기도 한다), 둘 다 비어도 종료 코드는 남긴다. 토큰은 지운 뒤 자른다."""
    text = redact(stderr.strip() or stdout.strip(), env)[-500:]
    return f"{text} (종료 코드 {returncode})" if text else f"종료 코드 {returncode}, 출력 없음"


def status_args(directory: Path | None = None) -> list[str]:
    """인증 확인 명령. 토큰 방식은 GH_TOKEN 계정만 본다(`--active`). 저장된 다른 계정의 상태로
    통과하거나 실패하지 않게 한다. gh 로그인 방식은 지금과 같다."""
    if uses_token(directory):
        return ["auth", "status", "--active"]
    return ["auth", "status"]


def alert_kind(directory: Path | None = None) -> Kind:
    """GitHub 인증이 실패했을 때 띄울 알림의 종류."""
    return "gh_token" if uses_token(directory) else "gh_login"


def hint(directory: Path | None = None) -> str:
    """시작 확인이 실패했을 때 보일 고치는 방법."""
    if uses_token(directory):
        return f"토큰이 만료·폐기됐으면 새 토큰을 {TOKEN_FILE} 파일에 넣는다"
    return "gh auth login"
