"""SQLite 테이블 정의. 연결과 쓰기는 각자 자기 쪽(워커는 worker/state.py)에서 한다."""

from datetime import datetime
from typing import Literal

from sqlmodel import Field, SQLModel

# error: 환경 오류가 아닌 예외(파이프라인 버그, 작업 시간 초과). 같은 커밋은 다시 넣지 않는다
RunState = Literal["running", "done", "cancelled", "env_error", "error"]


# open: 열려 있음, merged: 머지됨, superseded: 원본 PR에 새 커밋이 와서 파이프라인이 닫음,
# rejected: 사람이 머지하지 않고 닫음, closed_with_pr: 원본 PR이 닫혀 파이프라인이 닫음
ProposalState = Literal["open", "merged", "superseded", "rejected", "closed_with_pr"]


class Proposal(SQLModel, table=True):
    """제안 PR (DESIGN.md F10). 머지 여부는 커밋 메시지가 아니라 이 번호로 GitHub에서 확인한다."""

    id: int | None = Field(default=None, primary_key=True)
    repo: str = Field(index=True)
    pr_number: int = Field(index=True)  # 원본 PR
    skill: str
    head_sha: str  # 수정안을 만든 원본 PR의 커밋
    branch: str
    number: int  # 제안 PR 번호
    state: str  # ProposalState
    merge_commit: str | None = None
    created_at: datetime
    updated_at: datetime


class ProposalTarget(SQLModel, table=True):
    """제안 PR이 고친 위반 (파일, 규칙 코드, 줄 내용). 사람이 그 제안을 닫으면 다음 커밋에서
    같은 위반을 수정 대상에서 뺀다 (DESIGN.md F10, app/models/rejection.py)."""

    id: int | None = Field(default=None, primary_key=True)
    proposal_id: int = Field(index=True)
    file: str
    code: str
    line_text: str


class CommandRecord(SQLModel, table=True):
    """처리한 명령 코멘트. 같은 코멘트에 두 번 답하지 않는다 (DESIGN.md F11)."""

    comment_id: int = Field(primary_key=True)
    repo: str = Field(index=True)
    pr_number: int
    command: str
    result: str
    handled_at: datetime


class Run(SQLModel, table=True):
    """PR·커밋별 검사 한 번."""

    id: int | None = Field(default=None, primary_key=True)
    repo: str = Field(index=True)
    pr_number: int = Field(index=True)
    head_sha: str = Field(index=True)
    kind: str
    source: str
    state: str  # RunState
    test_status: str | None = None  # PytestRun.status
    comment_id: int | None = None
    started_at: datetime
    heartbeat_at: datetime
    finished_at: datetime | None = None


class AICall(SQLModel, table=True):
    """AI 호출 한 번 (DESIGN.md 비용 최적화). PR당 예산은 이 기록의 합으로 센다."""

    id: int | None = Field(default=None, primary_key=True)
    repo: str = Field(index=True)
    pr_number: int = Field(index=True)
    head_sha: str
    skill: str
    model: str
    attempt: int
    ok: bool
    error: str | None = None  # AIErrorKind
    cost_usd: float | None = None  # claude가 알려준 비용. 모르면 None
    charged_usd: float  # 예산에서 뺀 비용 (모르면 호출당 상한)
    permission_denials: int = 0  # 허용 범위 밖이라 거부된 도구 호출 수
    created_at: datetime | None = None  # Store가 기록할 때 채운다


class ModelCacheEntry(SQLModel, table=True):
    """해시 키 → 모델 결과 (DESIGN.md 5.6, 9.1). thread·PR과 무관하게 키가 같으면 같은 결과를 쓴다.

    키는 app/models/cache_key.py가 만든다.
    """

    __tablename__ = "model_cache"

    key: str = Field(primary_key=True)
    stage: str  # call_budget.Stage
    model: str  # 모델이 알려준 전체 id. 모르면 호출한 이름
    prompt_version: str
    result_json: str
    created_at: datetime


class ModelCall(SQLModel, table=True):
    """모델 호출 한 번 (DESIGN.md 7.8의 `ai_calls`, 9.2). 호출 상한은 `counted`의 합으로 센다.

    v1 수정 경로의 달러 기록 `AICall`(테이블 `aicall`)과 다른 테이블이다. 그 경로를 호출 수
    상한으로 옮기는 2단계에 `AICall`을 없앤다.
    """

    __tablename__ = "ai_calls"

    id: int | None = Field(default=None, primary_key=True)
    repo: str = Field(index=True)
    pr_number: int = Field(index=True)
    head_sha: str
    stage: str  # call_budget.Stage
    # 규칙 묶음 상한에서 센 호출이면 그 묶음의 캐시 키. PR 상한에서 센 호출이면 None
    ruleset_key: str | None = Field(default=None, index=True)
    model: str  # claude --model에 넘긴 이름
    model_id: str = ""  # 모델이 알려준 전체 id (별칭이 가리킨 것)
    tier: str  # call_budget.Tier
    attempt: int = 1
    counted: int  # 상한에서 센 값. 가벼운 모델 1, 무거운 모델 3, 저장된 결과 0
    cache_hit: bool = False
    # None: 호출을 시작했고 결과를 아직 적지 못함. 워커가 호출 도중 죽어도 센 값은 남는다
    ok: bool | None = None
    error: str | None = None
    input_tokens: int | None = None  # 캐시 생성·읽기를 합친 입력. 상한에는 쓰지 않는 측정값
    output_tokens: int | None = None
    permission_denials: int = 0
    created_at: datetime = Field(index=True)


class RulesetEntry(SQLModel, table=True):
    """규칙 묶음의 캐시 키 → 원문 확인을 거친 규칙 묶음 JSON (DESIGN.md 4.3, 7.8).

    `rules.disabled`는 적용하기 전의 묶음이다. 끈 규칙은 쓸 때 뺀다.
    """

    __tablename__ = "rulesets"

    key: str = Field(primary_key=True)
    ruleset_json: str
    created_at: datetime
