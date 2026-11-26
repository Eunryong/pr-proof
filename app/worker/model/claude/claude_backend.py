"""claude CLI로 하는 모델 구현 (docs/DESIGN.md 8.4 "인터페이스와 구현").

`model_backend.ModelBackend`의 세 호출을 지금의 claude CLI 호출에 그대로 잇는다.

- `ask`: `claude_cli.run_structured`를 도구 없이(`no_tools`, 빈 임시 폴더) 부른다.
- `reading`: `workdir.reading_folder`가 모델 폴더와 경로 감시 훅을 만들고, 그 안에서
  `run_structured`를 읽기 도구 셋으로(`read_tools`) 부른다. 호출마다 훅 기록 수를 도구 호출 수와
  견준다.
- `fix`: `workdir.run_in_model_folder`가 모델 폴더에서 `claude_cli.run_claude`를 파일 도구로
  (`file_tools`, 파이프라인 도구가 있으면 MCP 서버를 붙인 `fix_tools`) 부르고 바뀐 것을 worktree에
  적용한다. 로그인 풀림 같은 환경 오류는 결과의 `environment`에 적는다(`environment_problem`).
- `startup_check`: `claude auth status`(비용 없음)의 `loggedIn`, `loginExpiresAt`.

`--tools`·`--allowedTools` 목록, `--settings`의 훅 설정, `--mcp-config`와 중계, `--max-budget-usd`,
`--json-schema`가 더하는 답 받는 도구, stream-json 읽기는 모두 `claude_cli`·`workdir` 안에 있고
인터페이스에 드러나지 않는다.

모델 호출 함수는 부를 때마다 `claude_cli`에서 찾는다(`run_structured`, `run_claude`). 테스트가 그
이름을 바꿔 끼우면 그대로 쓰인다. 생성자의 `run_structured`, `run_claude`는 그 대신 쓸 함수다
(테스트용).
"""

import json
import subprocess
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from app.worker.model.claude import claude_cli, workdir
from app.worker.model.model_backend import (
    AskModel,
    Capabilities,
    EditCode,
    EditResult,
    PipelineTool,
    StartupCheck,
    StructuredResult,
)

NAME: Final = "claude-cli"
CAPABILITIES: Final = Capabilities(
    reads_code=True, reports_reads=True, edits=True, pipeline_tools=True, max_diff_chars=400_000
)
LOGIN_CHECK: Final = "Claude 로그인"
LOGIN_HINT: Final = "claude 실행 후 /login"
AUTH_TIMEOUT_S: Final = 30


def auth_status() -> str:
    """`claude auth status`의 JSON 출력. 비용이 들지 않는다. 실행하지 못하면 빈 문자열."""
    try:
        return subprocess.run(
            ["claude", "auth", "status"],
            capture_output=True,
            text=True,
            env=claude_cli.claude_env(),
            timeout=AUTH_TIMEOUT_S,
            check=False,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def login_ok(status_json: str, now: datetime) -> bool:
    """`loggedIn`이 참이고 `loginExpiresAt`이 지나지 않았으면 통과.

    만료 시각이 없으면(null) 로그인만 본다. 형식을 읽지 못하면 실패로 본다.
    """
    try:
        data = json.loads(status_json)
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict) or data.get("loggedIn") is not True:
        return False
    expires = data.get("loginExpiresAt")
    if expires is None:
        return True
    if not isinstance(expires, str):
        return False
    try:
        expires_at = datetime.fromisoformat(expires)
    except ValueError:
        return False
    if expires_at.tzinfo is None:
        return False
    return expires_at > now


def _timeout(timeout: float | None) -> dict[str, float]:
    """시간 상한을 준 호출만 넘긴다. 주지 않으면 claude_cli의 기본값이다."""
    return {} if timeout is None else {"timeout": timeout}


class ClaudeCliBackend:
    """claude CLI 구현. 상태를 갖지 않는다(폴더는 호출마다 만들고 지운다)."""

    name: Final = NAME
    capabilities: Capabilities = CAPABILITIES

    def __init__(
        self,
        *,
        run_structured: AskModel | None = None,
        run_claude: EditCode | None = None,
        auth_status: Callable[[], str] = auth_status,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._run_structured, self._run_claude = run_structured, run_claude
        self._auth_status, self._clock = auth_status, clock

    def _structured(self) -> AskModel:
        return self._run_structured or claude_cli.run_structured

    def ask(
        self,
        prompt: str,
        *,
        system_prompt: str,
        schema: Mapping[str, object],
        model: str,
        max_budget_usd: float,
        timeout: float | None = None,
    ) -> StructuredResult:
        return self._structured()(
            prompt, system_prompt=system_prompt, schema=schema, model=model,
            max_budget_usd=max_budget_usd, **_timeout(timeout),
        )  # fmt: skip

    def _ask_in_folder(self, prompt: str, **kwargs: object) -> StructuredResult:
        """`workdir.reading_folder`가 `read_in`(폴더와 훅 설정)을 더해 부른다."""
        return self._structured()(prompt, **kwargs)

    def reading(self, worktree: Path) -> AbstractContextManager[AskModel]:
        return workdir.reading_folder(worktree, self._ask_in_folder)

    def fix(
        self,
        worktree: Path,
        prompt: str,
        *,
        system_prompt: str,
        model: str,
        max_budget_usd: float,
        tools: Mapping[str, PipelineTool] | None = None,
        timeout: float | None = None,
    ) -> EditResult:
        result = workdir.run_in_model_folder(
            worktree, prompt, run=self._run_claude, tools=tools, system_prompt=system_prompt,
            model=model, max_budget_usd=max_budget_usd, **_timeout(timeout),
        )  # fmt: skip
        return replace(result, environment=claude_cli.environment_problem(result) or "")

    def startup_check(self) -> StartupCheck:
        ok = login_ok(self._auth_status(), self._clock())
        return StartupCheck(LOGIN_CHECK, ok, LOGIN_HINT, "claude_login")
