## 7. 데이터 모델

아래 타입은 데이터 정의이고 순수 모듈(이름이 `_pure.py`로 끝나고 외부 호출이 없는 모듈, 12.5)에 둔다. 아래는 필드의 뜻을 정하는 초안이고, 이름은 구현 때 기존 코드에 맞춘다.

### 7.1 규칙

```python
@dataclass(frozen=True)
class RuleSource:
    file: str            # "CONTRIBUTING.md", "pyproject.toml"
    section: str         # 문서의 제목 경로. 예: "코드 스타일 > 상수"
    blob_sha: str        # 읽은 파일의 git blob 해시

@dataclass(frozen=True)
class Rule:
    id: str                                   # 문서 규칙: R-<파일 약칭>-<원문 해시 8자>. 도구 규칙: 도구의 코드
    text: str                                 # 원문 인용 (출처 문서에 그대로 있음을 확인한 것)
    source: RuleSource
    origin: Literal["document", "tool_config"]
    scope: tuple[str, ...]                    # 적용 범위 glob. 비면 저장소 전체. 테스트 파일 제외 여부 포함
    check: CheckMethod                        # 확인 방법 (4.4)
    fix: Literal["mechanical", "model", "none"]
    examples: tuple[Example, ...] = ()        # 문서에 있던 예시 + 모델이 쓴 예시 (어느 쪽인지 표시)
    exclude: tuple[str, ...] = ()             # 범위가 없는 규칙에서 모델이 추정으로 뺀 경로 glob (4.3). 좁히기만
    exclude_reason: str = ""                  # 뺀 이유 한 문장. 코멘트에 보인다
    target: Literal["code", "pr", "run"] = "code"     # 확인 대상 (4.3). 2026-11-22에 human을 run으로
    runs: tuple[Literal["tests", "lint"], ...] = ()   # target이 run일 때 규칙이 말하는 실행 결과

@dataclass(frozen=True)
class CheckMethod:
    kind: Literal["tool", "checker", "model", "none"]
    tool: str = ""                            # "ruff", "ruff-format"
    params: Mapping[str, object] = field(default_factory=dict)   # 예: {"codes": ["N802"]}
    checker_id: str = ""
    reason: str = ""                          # none·model로 정한 이유

@dataclass(frozen=True)
class Example:
    code: str
    expect_lines: tuple[int, ...]             # 위반 예시면 잡혀야 할 줄, 정상 예시면 빈 튜플
    written_by: Literal["document", "model"]
```

규칙 묶음(`RuleSet`)은 규칙 목록, 원천 파일의 (경로, blob 해시) 목록, 파이프라인 버전, 모델, 읽지 못한 문서와 이유를 묶은 것이다.

### 7.2 검사기

```python
@dataclass(frozen=True)
class Checker:
    id: str                                   # 본문 해시
    rule_id: str
    rule_text_hash: str
    kind: Literal["ast-grep", "python-ast", "path-pattern"]
    body: str                                 # 규칙 YAML, 스크립트, 또는 경로 패턴 선언(JSON)
    examples: tuple[Example, ...]
    engine_version: str                       # 검사 엔진 버전. 바뀌면 예시 확인을 다시 한다
    model: str = ""
    verified_at: datetime | None = None
```

### 7.3 위반

```python
@dataclass(frozen=True)
class Violation:
    id: str                                   # (규칙 id, 파일, 줄 내용, 그 파일에서 몇 번째인지)의 해시. 줄 번호를 쓰지 않는다
    rule_id: str
    file: str
    line: int                                 # 파일 단위 위반이면 0
    message: str                              # 도구의 메시지 또는 모델의 why
    found_by: Literal["tool", "checker", "model"]
    code: str = ""                            # 도구의 규칙 코드
    line_text: str | None = None              # 앞뒤 공백을 뺀 줄 내용. 거절 기록과 맞춰 본다
    mechanical_fix: bool = False              # 도구가 동작을 바꾸지 않는 수정을 낼 수 있다
    covered: bool | None = None               # 테스트가 그 줄을 지나가는지. None은 모름
    rejected_in: int | None = None            # 같은 줄을 고친 수정안을 사람이 닫았으면 그 PR 번호
    unit: Literal["line", "file"] = "line"    # file: 파일의 이름·경로가 규칙을 어김 (경로 패턴 검사기)
```

지금의 `Finding`에서 `skill`이 `rule_id`와 `found_by`로 바뀐다.

PR 단위 위반(4.5 끝)은 이 타입이 아니다. `app/worker/rules/pr_check_pure.py`의 `PrViolation`(규칙 id, `where`: `commit` / `pr_title` / `pr_body` / `branch` / `changed_paths`, `found_by`: `pattern` / `model`, 커밋의 전체 해시, 인용, 이유)이고 파일과 줄이 없다. 수정 경로가 받지 않는다.

### 7.4 수정 판단

```python
SkipReason = Literal[
    "rejected_before", "notify_only", "tests_failed", "no_safety_net", "not_covered",
    "coverage_unknown", "team_pattern", "risky", "call_limit", "undecided",
]

@dataclass(frozen=True)
class FixDecision:
    violation_id: str
    action: Literal["fix", "skip"]
    decided_by: Literal["pipeline", "model"]
    method: Literal["mechanical", "model", ""] = ""
    reason: SkipReason | None = None
    why: str = ""                             # 코멘트에 보이는 한 줄
    evidence: tuple[int, ...] = ()            # 참고한 과거 기록 id
```

### 7.5 설명 JSON

수정 작성의 모델 호출이 수정과 함께 낸다. 모델의 답에는 줄 번호가 없다(2026-11-21, 11.1). 4.8의 4번이 확인하고 줄을 채운다.

```json
{
  "summary": "주문 할인 임계값을 모듈 상수로 추출",
  "changes": [
    {"file": "orders/services/pricing.py", "rule": "R-contrib-3fa9c2d1", "why": "임계값 50을 DISCOUNT_THRESHOLD로 정의하고 비교에서 그 상수를 쓴다"}
  ]
}
```

항목은 (파일, 규칙) 쌍마다 하나이고, 바꾼 코드 파일마다 하나 이상 둔다. 만든 테스트 파일은 넣지 않는다. 대상이 많아도 항목은 "파일 수 × 그 파일에서 고친 규칙 수"다. 옛 답의 `line`은 있어도 읽지 않는다.

2026-11-22부터 답에 `untested: [{file, rule, why}]`가 더해졌다. 테스트를 쓸 수 없는 대상 위반이 있는 (파일, 규칙)과 모델이 적은 까닭이다. 대상 규칙·대상 파일이 아닌 항목은 버리고, 없으면 빈 목록이다. 까닭은 "있던 테스트도 만든 테스트도 이 줄을 실행하지 않음" 표시 옆에 나온다(4.9).

검증을 통과한 설명은 `changes: [{file, line, rule, why}]`다. 파이프라인이 대상 위반의 줄을 바꾼 덩어리마다 그 위반의 규칙으로 하나씩 만든다. `line`은 고친 뒤 파일의 줄 번호(덩어리의 더한 줄 중 맨 앞, 지우기만 했으면 지운 자리의 앞줄이고 파일 맨 앞이면 뒷줄)이고 `why`는 그 (파일, 규칙)의 모델 글이다. `summary`는 수정안 PR 본문에, 채운 `changes`는 줄 리뷰 코멘트와 본문의 "모델의 설명"에 쓴다.

### 7.6 RAG 기록

```python
@dataclass(frozen=True)
class Record:
    id: int
    repo: str
    kind: Literal["adopted", "rejected", "failed", "checker"]
    origin: Literal["fix_pr", "author_commit", "verification", "ruleset", "eval"]
    rule_id: str
    rule_text: str
    file: str
    line_text: str
    context: str                              # 위반 줄과 앞뒤 5줄 (임베딩하는 글의 일부)
    diff: str                                 # 관련 hunk만, 최대 60줄. 검사기 기록이면 검사기 본문
    detail: str                               # 실패 기록: 실패 이유
    pr_number: int | None
    fix_pr_number: int | None
    commit_sha: str | None
    created_at: datetime
    embedded_at: datetime | None              # 벡터 저장소에 넣은 시각. 비어 있으면 아직 안 넣음
    embed_model: str | None
```

### 7.7 언어 프로필

언어마다 다른 것은 여기에만 둔다(2.3). 7.1–7.6의 모델과 5절의 그래프는 프로필을 통해서만 언어별 동작을 부른다. 아래 표는 지금 코드에 있는 것이고, 손으로 짠 Python 하나다. 방향은 이 칸들을 모델이 저장소를 보고 정한 것으로 채우는 것이다(2.3, pr-proof#107). 언어를 하나 더 손으로 짜 넣지 않는다.

| 프로필이 답하는 것 | Python 프로필 (처음이자 지금 유일한 구현) |
| --- | --- |
| 이 저장소가 이 언어인가, 무엇이 필요한가 | `pyproject.toml`, `requirements*.txt`, `ruff.toml` 등 base의 설정 파일. 돌릴 것이 있는지는 테스트를 돌리는 폴더(PR head)에 의존성 파일(`requirements*.txt`, `pyproject.toml`)이나 `.py`·`.pyi` 파일이 있는지로 본다(`python.has_work`, 이름만 본다). 없으면 테스트 상태가 `no_env`이다(4.5) |
| 런타임 | Python. 도구 캐시의 python-build-standalone 배포본(버전과 SHA256 고정, 3.11.18 하나). 저장소가 요구하는 버전으로 고르는 일은 아직 없다(11.1) |
| 도구 목록 (모두 밖에서 받아 오는 것) | `ruff`, `ruff-format` |
| 의존성 설치와 캐시 | `.venv` (4.5). 캐시 키에 런타임 버전 |
| 테스트 실행과 결과 읽기 | pytest, `report.json` |
| 커버리지 읽기와 줄 대응 | coverage.py의 `coverage.xml`, `ast`로 문장 범위 |
| 줄마다 실행한 테스트 (수정 작성의 도구와 출구 검사) | pytest-cov의 `--cov-context=test`와 `coverage.json`(`show_contexts`). `app/worker/lang/python.read_suite` |
| 만든 테스트의 자리와 이름 | `tests/`(없고 `test/`가 있으면 그것) 바로 아래의 새 `test_pipeline_<이름>.py`. 판정은 `app/worker/fix/scope_pure.is_generated_test` |
| 테스트 파일의 정의 | `tests/`·`test/` 아래, `test_*.py`, `*_test.py`, `conftest.py` |
| 검사기의 대안 형식 | Python AST 스크립트 |

3단계에서는 흩어져 있던 Python 전용 코드를 한 곳으로 모으기만 했다. 이 표의 칸을 모델이 정하게 하는 설계는 13절의 7단계에서 잡는다.

3단계(2026-11-20)에 `app/worker/lang/python.py`로 옮긴 것: 런타임 준비, `.venv` 설치, pytest 실행과 `report.json` 읽기, 위반 줄의 커버리지 읽기, ruff·ruff-format 실행. 순수 모듈(coverage.xml과 문장 범위 파싱 `app/worker/testing/coverage_pure.py`, 테스트 파일의 정의 `app/worker/fix/scope_pure.is_test_file`, ruff 설정·버전 읽기와 도구 목록 `app/worker/core/tools_pure.py`)은 프로필로 옮기지 않았다. 그것을 쓰는 판정(허용 범위, 도구 선택)도 순수 모듈이고, 순수 모듈은 실행 코드인 프로필을 import하지 않는다(12.5). 참고 지표 diff-cover(`app/worker/coverage.py`)는 체크포인트의 타입 경로를 지키려고 옮기지 않았다.

### 7.8 SQLite 테이블

| 테이블 | 내용 | 상태 |
| --- | --- | --- |
| `runs` | PR·커밋별 검사 이력, 상태(`running`, `done`, `cancelled`, `env_error`, `error`, `blocked`, `elsewhere`, `deferred`. `elsewhere`는 같은 저장소를 보는 다른 서버가 이 커밋을 이미 처리해 검사하지 않은 것, `deferred`는 다른 서버를 기다리며 미룬 것이고 둘 다 코멘트를 건드리지 않았다. 4.1, 4.9), 생존 확인 시각, 그 검사를 돌린 파이프라인 코드의 커밋(`pipeline_commit`, 2026-11-22. 4.1), 단계마다 걸린 시간(`stage_seconds`. 돈 순서대로 쌓은 `[[단계, 초], …]`이고 워커 로그에도 `단계 <이름>: N초`로 남는다) | 있음. 규칙 묶음 키를 더한다 |
| `proposals` | 수정안 PR, 묶음, 상태 | 있음. `skill`이 묶음(`mechanical`·`model`)이 된다 |
| `proposaltarget` | 수정안이 고친 위반의 (파일, 규칙, 줄 내용) | v1 lint 경로만 썼다. 3단계부터 쓰지 않는다(거절은 `records`). 예전 데이터베이스에 남은 테이블은 지우지 않는다 |
| `commandrecord` | 처리한 명령의 코멘트 ID | 있음 |
| `call_grants` | `/fix`가 PR 호출 상한을 한 번 더 준 기록: 저장소, PR, 커밋, 코멘트 ID (9.2) | 있음 (2026-11-20) |
| `ai_calls` | 모델 호출 한 번의 기록 | 있음. 환경 처방 상한에서 센 호출은 `ruleset_key`에 `env:<저장소>`를 적는다(2026-11-29). 4단계(2026-11-20)에 `judge`가 처음 쓰인다. `skill`이 단계(`rules`·`checker`·`judge`·`decide`·`fix`)가 되고, 입력·출력 토큰 수, 호출 상한에서 센 값, 캐시 적중 여부를 더한다. 달러 비용 칸은 API 백엔드일 때만 채운다 |
| `rulesets` | 캐시 키 → 규칙 묶음 | 새로 |
| `checkers` | 채택한 검사기와 실패한 시도 | 새로 |
| `env_plans` | base에서 확인한 환경 처방: 저장소, 설정의 모델 이름, 프롬프트 판, 처방 JSON, 기댄 경로 → base의 git 객체 해시, base 실행의 요약(4.5 "환경 처방 정하기") | 있음 (2026-11-29). 새 테이블이라 예전 데이터베이스는 그대로 열린다 |
| `model_cache` | 해시 키 → 모델 결과 (단계, 모델, 프롬프트 버전, 결과 JSON) | 있음. 모델 판단은 (규칙, hunk) 쌍마다 한 줄 (4단계, 2026-11-20). 테이블은 그대로다 |
| `feedback` | 처리한 PR 코멘트(4.11): 종류, 코멘트 id, 스레드, 글쓴이, 처리한 커밋, 판단(`accepted`·`rejected`·`no_action`·`deferred`·`error`), 까닭, 답글 id, 수정안 번호, 스레드를 해결한 때. (repo, kind, item_id)마다 하나 | 있음 (2026-11-22. `deferred`와 처리하며 바로 해결한 `resolved_at`은 둘째 덩어리) |
| `violations` | 검사별 위반. 다음 커밋에서 "작성자가 직접 고침"을 알아내는 데 쓴다 | 있음 (문서 규칙 위반, 2026-11-20) |
| `fix_decisions` | 위반별 수정 판단과 이유 | 새로 |
| `fix_attempts` | 수정 시도: 묶음, 회차, 모델, 검증 결과, 통과한 패치와 설명 JSON | 있음 (문서 규칙 경로, 2026-11-20. 고친 뒤 파일 해시와 대상 위반도 둔다. v1 경로는 `fixes/` 폴더의 패치 파일. 5단계에 검색으로 넣은 기록 id `references_json`(NULL 허용 열)을 더했다) |
| `records` | RAG 기록의 원본 | 있음 (채택·거절·실패, 2026-11-20. 검사기 기록과 `embedded_at`·`embed_model`은 5단계, 2026-11-20) |
| `rag_auto_off` | `rag.enabled: auto`가 검색을 다시 끈 저장소: 저장소(키), 끈 때의 기준값 둘(검색이 붙은 쪽 30, 견줄 쪽 10), 기록 수, 두 쪽의 채택·전체 수, 시각 (6.7) | 있음 (2026-11-20, `v2/rag-auto`). 새 테이블이라 예전 데이터베이스는 그대로 열린다(`test_existing_database_gets_rag_auto_off_and_keeps_its_rows`). 검색 유무별 채택률 자체는 저장하지 않고 `fix_attempts`와 `proposal`에서 그때마다 센다 |
| `eval_runs` | 평가 세트 실행 결과와 기준선 | 있음 (6단계, 2026-11-20). 실행 한 번이 한 줄이다: 기준선 여부, 이름, 파이프라인 커밋, 실제 모델 여부, 통과·실패·건너뜀 수, 케이스별 결과(JSON), 도구를 끈 평가의 조건/방법별 재현율·정밀도(JSON), 단계별 기본 모델과 프롬프트 판(JSON). `python -m app.worker.cli eval`이 쓴다. 새 테이블이라 예전 데이터베이스는 그대로 열린다 |
| `approvals` | 외부 작성자 PR의 승인: 저장소, PR, 커밋, 승인자, 코멘트 ID, 시각 (8.6) | 있음 (6단계, 2026-11-20). `runs`에는 같은 날 PR 작성자 열 네 개(판단에 쓴 값, 작업에 실린 값)와 상태 `blocked`를 더했다. 예전 데이터베이스는 열을 더해 그대로 연다 |

새 테이블과 NULL 허용 열은 워커가 시작할 때(preflight 전) 만든다. 웹 쪽 동기화는 같은 DB를 읽기만 하므로, 첫 작업에서야 만들면 그 사이 동기화가 주기마다 실패한다(2026-11-20 배포). 맞추지 못하면(쓸 수 없는 경로, 더할 수 없는 NULL 불가 열) 워커는 로그 한 줄을 남기고 1로 끝난다. api가 먼저 떠서 첫 동기화가 옛 DB를 읽으면 그 주기만 실패하고 다음 주기에 읽는다.

### 7.9 만든 테스트와 위반 줄의 실행 상태

수정 작성의 모델이 만든 테스트와 출구 검사의 판정이다(4.7, 4.8의 6번, 2026-11-22). 판정 규칙은 `app/worker/testing/generated_tests_pure.py`에 있고 외부 호출이 없다.

```python
Rejection = Literal["empty", "too_many", "too_big", "not_run", "fails_before",
                    "misses_violation", "overlaps"]
Outcome = Literal["passed", "failed", "not_run"]

@dataclass(frozen=True)
class GeneratedTest:                       # 만든 테스트 파일 하나
    file: str
    lines: int
    before: Outcome                        # 고치기 전 코드에 얹어 돈 결과
    covers: tuple[str, ...] = ()           # 고치기 전에 실행한 대상 위반 id (실행으로 정한다)
    new_lines: int = 0                     # 있던 테스트와 앞서 받아들인 테스트가 실행하지 않던 줄 수
    after: Outcome | None = None           # 고친 뒤 결과. 고치기 전에 떨어졌으면 None
    rejected: Rejection | None = None      # 이름 붙은 탈락 이유. None이면 받아들임

LineStatus = Literal["existing", "generated", "none", "unknown"]

@dataclass(frozen=True)
class LineRun:                             # 대상 위반 하나의 실행 상태 (4.9의 표시)
    violation_id: str
    status: LineStatus                     # 있던 테스트 / 만든 테스트 / 어느 것도 아님 / 모름
    why: str = ""                          # none·unknown일 때 모델이 적은 까닭 (7.5의 untested)

@dataclass(frozen=True)
class ToolUsage:                           # 수정 작성 호출들 안의 도구 호출 (`app/worker/model/mcp_pure.py`)
    check_rules: int = 0
    run_tests: int = 0
    seconds: float = 0.0
    limited: bool = False                  # 상한에 닿아 돌리지 않은 호출이 있었다
```

`ModelFix`(5.5의 `fixes["model"]`)는 `generated`, `line_runs`, `tool_usage`를 가진다. 판정에 쓰는 테스트 결과는 `Suite`(테스트 파일마다 결과와 수, 대상 위반이 있는 파일의 줄마다 실행한 테스트 파일)이고, 그래프 상태에는 넣지 않는다.

### 7.10 환경 처방

저장소를 빌드·테스트할 환경을 데이터로 적은 것이다(`app/worker/env/recipe_pure.py`, 2026-11-29). 사람이 쓴 JSON 파일은 수동 명령 `python -m app.worker.cli env-try <처방.json> <저장소 폴더>`가 받는다. 모델이 쓰고 base에서 확인하는 처방은 수동 명령 `env-plan`이 정한다(4.5 "환경 처방 정하기"). 흐름에 잇는 일은 13절 7단계의 다음 PR들이 한다.

```text
EnvRecipe
├─ system: SystemLayer          시작 이미지 image, root로 돌릴 commands, git 받기 fetches
│   └─ fetches: GitFetch        url(https만), rev(40자리 커밋), dest(/opt/env 아래 상대 경로)
├─ deps: DepsLayer | None       저장소에서 복사해 넣을 inputs, 호스트 UID로 돌릴 commands
├─ overlays: Overlay            path, content, why. /opt/env/overlay/<path>에 쓴다
├─ env: (이름, 값)               빌드·테스트 단계의 환경 변수
├─ build: Step(name, command)   차례로 돌리고 처음 실패에서 멈춘다
├─ test: TestStep | None        report는 junit(결과 파일 .pipeline-out/<이름>) 또는 exit
└─ outputs                      빌드 산출물 폴더 (저장소 기준)
```

- **경계의 검사.** `validate`가 믿지 않는 JSON을 한 번 검사해 `EnvRecipe`나 고칠 곳의 목록(한국어, `칸: 까닭`)을 돌려준다. 이미지 참조의 모양, https 주소, 40자리 커밋, 상대 경로(`..`, `.`, 빈 조각이 없고 `-`나 `/`로 시작하지 않음), 환경 변수 이름, 단계 이름(`^[a-z0-9][a-z0-9_-]{0,31}$`, build와 test에서 겹치지 않음), 결과 파일 자리, 개수와 길이의 상한, 모르는 칸을 본다. 덮어쓰기 파일은 의존성 층에 쓰므로 `deps`가 있어야 한다.
- **층.** 시스템 층은 시작 이미지 위에서 root로 `system.commands`, `system.fetches`를 돌리고 `/opt/env`를 호스트 UID에게 준다. 의존성 층은 그 위에서 호스트 UID로 `deps.inputs`를 `/opt/env/src/<경로>`에, `overlays`를 `/opt/env/overlay/<경로>`에 복사해 넣고 `/opt/env/src`에서 `deps.commands`를 돌린다. 덮어쓰기 파일은 저장소에 쓰지 않는다. 층 이미지는 `pr-proof-env:<키>`이고 라벨 `pr-proof.env=1`을 단다.
- **층의 키.** 시스템 층은 (시작 이미지 digest, `system`의 JSON, 호스트 UID, 굽는 방법의 판)의 sha256이다. 의존성 층은 (시스템 층 키, `deps`의 JSON, 호스트 UID, 입력 파일 내용의 해시, 덮어쓰기 파일의 경로와 내용, 굽는 방법의 판)의 sha256이다. 덮어쓰기 파일의 `why`, `env`, `build`, `test`는 층의 키에 들어가지 않는다. 처방 전체의 키(`recipe_key`)는 따로 있다.
- **테스트 단계의 판정.** junit이면 결과 파일이 없거나 읽히지 않을 때 `unrunnable`(까닭에 종료 코드), 테스트가 0개면 `no_tests`, 실패·오류가 하나라도 있으면 `failed`, 모두 통과하고 0으로 끝나면 `passed`, 모두 통과했는데 0이 아니면 `unrunnable`이다. exit이면 0은 `passed`, 그 밖은 `failed`이고 결과 요약이 없다. 테스트 단계가 없으면 `no_tests`다. 상태는 7단계 첫 PR의 순서(취소, 시간 초과, 빌드 실패, 테스트 단계의 판정)로 정한다.
- **처방에 없는 칸.** 네트워크, 마운트, UID, 자원·시간 상한은 프로그램이 정한다(`env/install.py`의 `Limits`, 8.1).
