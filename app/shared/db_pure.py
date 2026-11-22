"""SQLite 테이블 정의. 연결과 쓰기는 각자 자기 쪽(워커는 app/worker/state.py)에서 한다."""

from datetime import datetime
from typing import Literal

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel

# error: 환경 오류가 아닌 예외(파이프라인 버그, 작업 시간 초과). 같은 커밋은 다시 넣지 않는다
# blocked: 신뢰되지 않은 작성자의 PR이라 승인 안내만 달고 끝냈다 (8.6). 같은 커밋은 다시 넣지 않는다
RunState = Literal["running", "done", "cancelled", "env_error", "error", "blocked"]


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
    # 이 수정안이 올린 수정 시도 (`fix_attempts`). 머지·닫힘의 기록이 읽는다. v1 lint의 수정안은
    # 없다(그 경로가 쓰던 `proposaltarget` 테이블은 3단계에 쓰지 않게 되었고 지우지 않는다)
    attempt_id: int | None = None


class CommandRecord(SQLModel, table=True):
    """처리한 명령 코멘트. 같은 코멘트에 두 번 답하지 않는다 (DESIGN.md F11)."""

    comment_id: int = Field(primary_key=True)
    repo: str = Field(index=True)
    pr_number: int
    command: str
    result: str
    handled_at: datetime


# accepted: 받아들여 수정안을 올림, rejected: 받아들이지 않고 이유를 답함, no_action: 고칠 것이
# 없는 코멘트(칭찬, 질문 없음 등), error: 처리하지 못함
# deferred: 받아들였지만 지금은 고칠 수 없음(테스트가 실패했거나 없음, 포크 PR, 이 커밋에 이미 열린
# 코멘트 수정안). deferred와 error는 PR에 새 커밋이 오면 다시 본다 (feedback_pure.handled)
FeedbackVerdict = Literal["accepted", "rejected", "no_action", "deferred", "error"]


class Feedback(SQLModel, table=True):
    """처리한 PR 코멘트 (Copilot 리뷰, 사람의 리뷰). 같은 코멘트를 두 번 처리하지 않는다.

    item_id는 줄 코멘트면 스레드의 첫 코멘트의 database id, 리뷰면 리뷰 id, 일반 코멘트면 코멘트
    id다. 줄 코멘트만 thread_id(GraphQL 노드 id)가 있고, 수정안이 머지되면 그 스레드를 해결됨으로
    바꾼다(resolved_at).
    """

    __table_args__ = (UniqueConstraint("repo", "kind", "item_id"),)

    id: int | None = Field(default=None, primary_key=True)
    repo: str = Field(index=True)
    pr_number: int = Field(index=True)
    kind: str  # FeedbackKind
    item_id: int
    thread_id: str | None = None
    login: str
    head_sha: str  # 처리할 때의 PR head
    verdict: str  # FeedbackVerdict
    why: str = ""
    reply_id: int | None = None  # 단 답글의 id
    proposal_number: int | None = None  # 받아들여 올린 수정안 PR 번호
    resolved_at: datetime | None = None
    handled_at: datetime


class CallGrant(SQLModel, table=True):
    """`/fix`가 PR 호출 상한을 한 번 더 준 기록 (DESIGN.md 4.9 명령, 9.2 "걸렸을 때").

    명령 코멘트 하나에 한 번이라 같은 명령을 다시 처리해도(재시도) 늘지 않는다. 그 PR의 기록 수가
    수정 작성의 시도 세대다."""

    __tablename__ = "call_grants"

    comment_id: int = Field(primary_key=True)
    repo: str = Field(index=True)
    pr_number: int = Field(index=True)
    head_sha: str
    created_at: datetime


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
    # 이 검사를 돌린 파이프라인 코드의 커밋 (`app/shared/version.py`). 이 열이 생기기 전의 줄은 NULL
    pipeline_commit: str | None = None
    # PR 작성자 (8.6). 판단에 쓴 값은 워커가 GitHub의 PR 조회에서 읽은 것이고, `job_` 쪽은 작업을
    # 만든 경로가 `PRJob`에 실은 기록용 값이다. 둘이 다르면 여기서 알 수 있다
    author_association: str | None = None
    author_login: str | None = None
    job_author_association: str | None = None
    job_author_login: str | None = None


class Approval(SQLModel, table=True):
    """외부 작성자 PR의 승인 (DESIGN.md 8.6). 승인은 적힌 커밋 하나에만 적용한다.

    워커가 `/recheck <커밋>`을 처리하며, 승인자의 권한과 커밋을 GitHub에서 읽은 값으로 확인한
    뒤에만 쓴다. 같은 코멘트를 다시 처리해도(재시도) 한 줄이다.
    """

    __tablename__ = "approvals"

    comment_id: int = Field(primary_key=True)
    repo: str = Field(index=True)
    pr_number: int = Field(index=True)
    head_sha: str  # 전체 해시
    approver: str  # 승인자의 로그인 이름
    created_at: datetime


class EvalRun(SQLModel, table=True):
    """평가 세트 한 번의 결과 (DESIGN.md 7.8의 `eval_runs`, 13절 6단계). `baseline`인 가장 최근 줄이
    비교의 기준선이다. 내용의 모양은 app/worker/eval_run_pure.py에 있다."""

    __tablename__ = "eval_runs"

    id: int | None = Field(default=None, primary_key=True)
    created_at: datetime
    label: str = ""
    baseline: bool = Field(default=False, index=True)
    pipeline_commit: str = ""  # 평가한 파이프라인 코드의 커밋. 모르면 빈 문자열
    real_model: bool  # False면 케이스의 가짜 모델이다(하네스와 결정적 도구만 확인한다)
    passed: int
    failed: int
    skipped: int
    cases_json: str  # eval_run.CaseResult 목록
    tools_off_json: str  # 도구를 끈 평가의 {조건/방법: {recall, precision}}. 없으면 "{}"
    models_json: str  # 단계별 기본 모델
    prompts_json: str  # 단계별 프롬프트 판


class ModelCacheEntry(SQLModel, table=True):
    """해시 키 → 모델 결과 (DESIGN.md 5.6, 9.1). thread·PR과 무관하게 키가 같으면 같은 결과를 쓴다.

    키는 app/worker/cache_key_pure.py가 만든다.
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

    v1 static 수정 경로의 달러 기록 테이블 `aicall`은 2단계에 그 경로와 함께 없앴다. 예전
    데이터베이스에 남은 그 테이블은 읽지도 지우지도 않는다.
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


class CheckerEntry(SQLModel, table=True):
    """규칙별 검사기 작성 결과 (DESIGN.md 4.4, 7.8): 채택한 검사기, 모델 판단으로 보낸 기록(그
    까닭), 또는 끝내 통과하지 못한 경로 규칙의 기록.

    키는 app/worker/checker_authoring_pure.py의 `checker_key`다. 회차마다의 답(초안)은
    `model_cache`에 있고, attempts_json의 각 회차 `artifact`가 그 키다. 호출 상한·모델 호출 실패로
    끝난 작성은 저장하지 않으므로 다음 검사가 이어 한다.
    """

    __tablename__ = "checkers"

    key: str = Field(primary_key=True)
    rule_id: str = Field(index=True)
    accepted: bool
    checker_json: str | None = None  # 채택한 Checker. 실패했으면 None
    detail: str = ""  # 실패한 이유 (마지막 회차)
    attempts_json: str  # app/worker/loop.py의 Attempt 목록
    created_at: datetime
    # 채택하지 않고 모델 판단으로 보낸 까닭 (app/worker/checker_authoring_pure.py의 `JudgeRoute`).
    # 경로 규칙과 도구 선택의 줄은 없다
    judge_route: str | None = None


class FixAttempt(SQLModel, table=True):
    """수정 묶음 하나의 시도 결과 (DESIGN.md 4.8, 7.8). 검증을 통과했으면 그 패치, 설명, 고친 뒤
    파일의 해시가 있다. 전달이 실패해 다시 올릴 때는 모델을 부르지 않고 이 패치를 새 worktree에
    적용한 뒤 해시를 맞춰 본다."""

    __tablename__ = "fix_attempts"

    id: int | None = Field(default=None, primary_key=True)
    repo: str = Field(index=True)
    pr_number: int = Field(index=True)
    head_sha: str  # 검증한 원본 PR의 커밋
    merge_base: str  # 바뀐 줄을 다시 센 기준
    bundle: str  # 수정 묶음 ("mechanical", "model")
    status: str  # model_fix.ModelFixStatus 또는 fix.MechanicalStatus
    attempts: int  # 작성 회차
    models_json: str  # 쓴 모델 목록 (JSON 배열)
    detail: str = ""  # 실패 이유
    targets_json: str  # records.Target 목록 (JSON)
    patch: str | None = None  # 검증을 통과한 패치 (`git diff HEAD`). 통과하지 못했으면 None
    explanation_json: str | None = None
    file_hashes_json: str | None = None  # 고친 뒤 바뀐 파일의 내용 해시 {경로: 해시}
    created_at: datetime
    # 검색으로 프롬프트에 넣은 과거 기록 id (JSON 배열). 검색 유무로 채택률을 나눠 센다 (6.7)
    references_json: str | None = None


class RagAutoOff(SQLModel, table=True):
    """`rag.enabled: auto`가 검색을 다시 끈 저장소 (DESIGN.md 6.7 "검색을 켜는 조건").

    검색을 붙인 수정안의 채택률이 낮았던 때의 숫자를 남긴다. 이 줄이 있는 동안 auto는 검색을 켜지
    않는다(끄면 검색을 붙인 쪽의 숫자가 더 늘지 않아, 숫자만으로 다시 정하면 같은 근거로 켜고 끄기를
    되풀이한다). 비교 기준값(min_attempts, min_baseline)이 바뀌면 이 줄은 쓰지 않고 다시 정한다.
    저장소마다 한 줄.
    """

    __tablename__ = "rag_auto_off"

    repo: str = Field(primary_key=True)
    min_attempts: int  # 끈 때의 비교 기준값 (검색을 넣은 쪽)
    min_baseline: int  # 끈 때의 견줄 쪽 최소 수
    records: int
    with_adopted: int
    with_known: int
    without_adopted: int
    without_known: int
    created_at: datetime


class SeenViolation(SQLModel, table=True):
    """검사 한 번에서 찾은 문서 규칙 위반 (DESIGN.md 7.8의 `violations`). 다음 커밋의 검사가
    "작성자가 직접 고침"을 알아내는 데 쓴다. 같은 커밋을 다시 검사하면 그 커밋의 줄을 바꿔 쓴다."""

    __tablename__ = "violations"

    id: int | None = Field(default=None, primary_key=True)
    repo: str = Field(index=True)
    pr_number: int = Field(index=True)
    head_sha: str = Field(index=True)
    violation_id: str
    rule_id: str
    file: str
    line: int
    line_text: str | None = None
    message: str
    unit: str  # Violation.unit
    context: str = ""  # 위반 줄과 앞뒤 5줄 (records.context_around)
    created_at: datetime


class Record(SQLModel, table=True):
    """팀의 과거 기록 원본 (DESIGN.md 4.10, 6.5, 7.6). 저장소의 글이 들어 있어 믿지 않는 자료다.

    같은 일을 두 번 넣지 않는다: (repo, kind, source_id)가 고유 키다. 검색(임베딩)은 5단계에
    켠다. 그 전에는 `embedded_at`이 비어 있다.
    """

    __tablename__ = "records"
    __table_args__ = (UniqueConstraint("repo", "kind", "source_id"),)

    id: int | None = Field(default=None, primary_key=True)
    repo: str = Field(index=True)
    kind: str  # records.RecordKind
    origin: str  # records.RecordOrigin
    source_id: str  # 원천 id. 예: "fix_pr:901:<위반 id>"
    rule_id: str = Field(index=True)
    rule_text: str
    file: str
    line_text: str
    context: str  # 위반 줄과 앞뒤 5줄
    diff: str  # 관련 hunk만, 최대 60줄
    detail: str = ""  # 실패 기록: 실패 이유
    pr_number: int | None = Field(default=None, index=True)
    fix_pr_number: int | None = None
    commit_sha: str | None = None
    created_at: datetime
    embedded_at: datetime | None = None
    embed_model: str | None = None
