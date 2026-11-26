"""모델 호출의 인터페이스 (docs/DESIGN.md 8.4 "인터페이스와 구현").

호출하는 쪽(규칙 읽기, 검사기 작성, 위반 판단, PR 설명 확인, 이슈 확인, 수정 판단, 수정 작성,
코멘트 판단 등)은 이 모듈의 `ModelBackend`와 결과 타입만 안다. 인터페이스는 "이 도구 목록으로
불러라"가 아니라 "도구 없이 이 꼴로 답해라"(`ask`), "이 폴더를 읽고 이 꼴로 답해라"(`reading`),
"이 폴더에서 고쳐라"(`fix`)의 높이로 긋는다. 안전을 지키는 방법은 구현마다 다르므로(claude CLI는
`--tools`와 경로 감시 훅, API는 파이프라인이 만든 도구, Codex는 자체 샌드박스) 구현 안에 둔다.
지금 구현은 claude CLI 하나다(`app/worker/model/claude/claude_backend.py`). 이름으로 구현을 고르는
곳은 `app/worker/model/backends.py`다.

구현이 지켜야 하는 조건 (계약, `tests/worker/model/test_model_backend_contract.py`):

1. `ask`: 모델에게 파일·명령·웹 도구가 없다. 저장소의 파일과 설정이 보이지 않는 곳에서 돈다.
2. `reading`: 연 폴더(`workdir.model_folder`가 worktree의 일반 파일만 복사한 것) 밖을 읽지 못한다
   (밖의 절대 경로, 밖을 가리키는 링크). 명령을 실행하지 못하고, 폴더에서 무엇이 바뀌어도 worktree에
   적용하지 않는다. 결과의 `reads`에 모델이 읽은 경로(폴더 기준, 폴더 전체는 ".")를, `tool_uses`에
   읽기 도구를 부른 횟수를 넣는다. 읽은 곳을 하나하나 알 수 없는 구현은 `reads`에 "."을 넣는다:
   저장한 판단은 읽은 곳이 모두 그대로일 때만 다시 쓰므로(4.5) 비워 두면 바뀐 코드에 옛 판단을 쓴다.
   `max_tool_uses`를 넘으면 멈추고 `TOOL_CAP`으로 돌려준다.
3. `fix`: 모델 전용 폴더 밖을 읽거나 쓰지 못하고, 명령을 실행하지 못한다. 모델이 부를 수 있는 것은
   파일 도구와 넘겨받은 파이프라인 도구(`tools`)뿐이다. 도구는 돌기 직전에 폴더의 지금 상태를
   worktree에 맞춘 뒤 부른다. 끝나면 바뀐 것을 diff로 worktree에 적용하고(링크 너머에는 쓰지
   않는다, 맞지 않으면 `APPLY`) 적용한 diff를 `applied`에 담는다. 어느 파일을 바꿔도 되는지는
   호출하는 쪽의 출구 검사(4.8)가 본다. 안전 장치가 실제로 돌았는지 확인하지 못하면 바뀐 것을 모두
   되돌리고 `TOOLS`로 돌려준다.
4. 모두: 지시는 `system_prompt`에만 있다. `prompt`, 모델이 읽은 파일, 도구 결과는 믿지 않는 글로
   다룬다(지시 자리에 넣지 않는다). 호출당 예산(`max_budget_usd`)과 시간 상한(`timeout`, None이면
   구현의 기본값)을 넘기지 않는다. PR과 관계없이 구현을 쓸 수 없는 상태(로그인 풀림, 명령 없음)면
   `fix`의 결과 `environment`에 그 까닭을 적는다. 호출하는 쪽은 그것을 환경 오류로 다시 시도한다.
"""

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol

from app.shared.ops_alert_pure import Kind
from app.worker.model.mcp_pure import ToolUsage

# 파이프라인 도구가 든 수정 작성 호출의 시간 상한(초)
# 모델 600초 + 도구 시간 상한 900초 + 여유 300초
FIX_TOOLS_TIMEOUT_S: Final = 1800.0


class AIErrorKind(StrEnum):
    """호출이 실패한 종류. 값은 호출 기록(`ai_calls`)의 오류 글에 그대로 들어간다."""

    TIMEOUT = "timeout"
    LAUNCH = "launch"  # 구현을 실행하지 못함 (명령 없음, 폴더 없음)
    EXIT = "exit"  # 구현이 오류로 끝남 (로그인 풀림 포함)
    BAD_JSON = "bad_json"  # 답을 읽지 못함
    BUDGET = "budget"  # 호출당 예산을 넘김
    # 모델에 보인 도구가 기대와 다르거나 안전 장치(경로 감시 등)가 돌지 않음. 결과를 쓰지 않는다
    TOOLS = "tools"
    TOOL_CAP = "tool_cap"  # 읽기 도구를 부른 횟수가 호출 하나의 상한을 넘음
    APPLY = "apply"  # 모델 폴더의 변경을 worktree에 적용하지 못함
    USAGE_LIMIT = "usage_limit"  # 계정의 사용량 한도에 닿아 답하지 못함


@dataclass(frozen=True)
class PermissionDenial:
    """허용 범위 밖이라 거부된 도구 호출 한 건 (worktree 밖 Read 등)."""

    tool_name: str
    tool_input: Mapping[str, object]


@dataclass(frozen=True)
class EditResult:
    """고치기(`fix`)의 결과. ok는 호출이 정상으로 끝났다는 뜻일 뿐, 수정안의 판정이 아니다.

    cost_usd가 None이면 비용을 알 수 없다 (시간 초과, 답 없음). 상한은 호출 수로 세므로 이 값은
    판단에 쓰지 않는다 (DESIGN 9.2).
    """

    ok: bool
    error: AIErrorKind | None
    cost_usd: float | None
    num_turns: int
    permission_denials: tuple[PermissionDenial, ...]
    text: str  # 모델의 마지막 답
    detail: str = ""
    # 모델이 부르려 한, 허용 목록 밖 도구의 (이름, 횟수)
    unexpected_tools: tuple[tuple[str, int], ...] = ()
    # 모델이 부른, 목록 안 도구의 횟수. 안전 장치가 호출마다 돌았는지 맞춰 보는 데 쓴다
    tool_uses: int = 0
    model_id: str = ""  # 별칭이 가리킨 전체 모델 id. 모르면 ""
    input_tokens: int | None = None  # 캐시 생성·읽기를 합친 실제 입력. 모르면 None
    output_tokens: int | None = None
    applied: bytes = b""  # 모델 폴더에서 worktree에 적용한 diff
    tool_usage: ToolUsage = ToolUsage()  # 파이프라인 도구를 부른 횟수와 시간
    # PR과 관계없이 구현을 쓸 수 없는 상태면 그 까닭(로그인 풀림 등). 호출하는 쪽은 환경 오류로 본다
    environment: str = ""
    # error가 USAGE_LIMIT일 때 한도가 풀리는 시각(초 단위 epoch). 모르면 None
    limit_resets_at: float | None = None


@dataclass(frozen=True)
class StructuredResult:
    """정해진 꼴의 답을 받는 호출(`ask`, `reading`)의 결과. ok이면 data에 스키마로 받은 객체가 있다
    (형식 확인은 부른 쪽이 한다).

    model_id는 별칭이 가리킨 전체 모델 id다. 모르면 "".
    input_tokens는 캐시 생성·읽기를 합친 실제 입력이다. 모르면 None.
    tool_uses(읽기 도구를 부른 횟수, 답을 내는 도구는 빼고)와 reads(모델이 읽은 경로. 폴더 기준이고
    폴더 전체는 ".")는 `reading`의 호출에서만 찬다.
    """

    ok: bool
    error: AIErrorKind | None
    data: Mapping[str, object] | None
    model_id: str
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    permission_denials: int
    detail: str = ""
    tool_uses: int = 0
    reads: tuple[str, ...] = ()
    unexpected_tools: tuple[tuple[str, int], ...] = ()  # 목록 밖 도구의 (이름, 횟수)
    num_turns: int = 0
    # error가 USAGE_LIMIT일 때 한도가 풀리는 시각(초 단위 epoch). 모르면 None
    limit_resets_at: float | None = None


class AIEnvironmentError(RuntimeError):
    """모델 구현을 쓸 수 없다 (명령 없음, 로그인 풀림). PR 탓이 아니라 환경 오류로 재시도한다."""


@dataclass(frozen=True)
class Capabilities:
    """구현이 할 수 있는 것. 호출하는 쪽은 이것을 보고 할 수 없는 일을 까닭과 함께 건너뛴다."""

    reads_code: bool  # `reading`: 폴더를 읽고 답한다
    reports_reads: bool  # `reading`의 결과에 읽은 경로를 하나하나 적는다 (못 하면 "."만 적는다)
    edits: bool  # `fix`: 폴더에서 고친다
    pipeline_tools: bool  # `fix`에 파이프라인 도구(규칙 검사, 테스트)를 줄 수 있다
    # 호출 하나에 자르지 않고 통째로 넣을 수 있는 번호 붙인 diff의 글자 수 상한 (pr-proof#156)
    max_diff_chars: int = 120_000
    # 검사기 작성 최대 시도 횟수. 로컬/경량 모델은 1회 실패 시 바로 모델 판단으로 넘긴다
    max_checker_attempts: int = 3


@dataclass(frozen=True)
class StartupCheck:
    """워커 시작 전 확인 한 줄 (`app/worker/preflight.py`). 구현을 쓸 수 있는 상태인지."""

    name: str  # 확인의 이름 ("Claude 로그인")
    ok: bool
    hint: str  # 실패했을 때 사람이 할 일
    alert: Kind  # 실패하면 띄우는 운영 알림의 종류 (6-4)


# 파이프라인 도구 하나: (인자) → 모델에게 돌려줄 글
# (`app/worker/model/claude/tool_server.py`의 Handler)
PipelineTool = Callable[[Mapping[str, str]], str]
# (사용자 메시지, *, system_prompt, schema, model, max_budget_usd) → 결과. `ModelBackend.ask`의
# 모양이다. `reading`이 주는 함수는 여기에 max_tool_uses를 더 받는다
AskModel = Callable[..., StructuredResult]
# worktree → 그 코드를 읽을 수 있는 폴더를 열고 그 안에서 묻는 함수를 주는 블록.
# `ModelBackend.reading`의 모양이다
ReadCode = Callable[[Path], AbstractContextManager[AskModel]]
# (worktree, 사용자 메시지, *, system_prompt, model, max_budget_usd, tools, timeout) → 결과.
# `ModelBackend.fix`의 모양이다
EditCode = Callable[..., EditResult]


class ModelBackend(Protocol):
    """모델 구현 하나. 위 계약을 지킨다."""

    @property
    def name(self) -> str:
        """구현의 이름 (`backends.make_backend`가 받는 이름)."""
        ...

    @property
    def capabilities(self) -> Capabilities: ...

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
        """도구 없이 묻고 schema의 꼴로 답을 받는다."""
        ...

    def reading(self, worktree: Path) -> AbstractContextManager[AskModel]:
        """worktree의 코드를 읽을 수 있는 폴더를 한 번 열고, 그 안에서 읽기 도구로 묻는 함수를 준다.

        함수는 `ask`의 인자에 `max_tool_uses`(읽기 도구를 부를 수 있는 횟수)를 더 받는다. 블록 안의
        호출들이 폴더 하나를 같이 쓰고, 블록이 끝나면 폴더를 지운다."""
        ...

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
        """worktree를 복사한 모델 전용 폴더에서 고치고, 바뀐 것을 worktree에 적용한다. tools(이름 →
        파이프라인 도구)가 있으면 모델이 고치는 동안 부를 수 있다."""
        ...

    def startup_check(self) -> StartupCheck:
        """이 구현을 지금 쓸 수 있는지 (비용이 들지 않는 확인)."""
        ...


def fix_unavailable(backend: ModelBackend) -> str:
    """수정 작성(파이프라인 도구를 준 고치기, 4.7)을 이 구현으로 할 수 없으면 그 까닭, 할 수 있으면
    "". 까닭이 있으면 호출하는 쪽은 모델을 부르지 않고 수정안을 만들지 않은 까닭으로 알린다."""
    caps = backend.capabilities
    if not caps.edits:
        return f"모델 구현 {backend.name}은 코드를 고치지 못한다"
    if not caps.pipeline_tools:
        return f"모델 구현 {backend.name}은 고치는 동안 규칙 검사·테스트 도구를 주지 못한다"
    return ""
