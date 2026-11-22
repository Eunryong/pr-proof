### 12.3 v1 절 번호 대응

| v1 | v2 |
| --- | --- |
| F1 웹훅, F2 동기화, F3 큐와 워커 | 4.1 |
| F4 PR 코드 준비 | 4.2 |
| F5 테스트 실행 | 4.5 (테스트 기준선), 8.1 |
| F6 위반 검출 | 4.3, 4.4, 4.5 |
| F7 수정 루프 | 4.7, 4.8, 5.3 |
| F8 안전망 | 4.5 (커버 여부), 4.6 (테스트 상태 조건), 4.8의 6번 (만든 테스트) |
| F9 결과와 PR 코멘트 | 4.9 |
| F10 제안 PR | 4.8 (비밀값, 상한), 4.9 (수정안 PR, 상태 관리) |
| F11 수동 명령 | 4.9 (명령) |
| F12 운영 알림 | 13절 6단계 |
| F13 선택 확장 | 버림 (12.4) |
| 설정 | 4.3, 10 |
| 보안·인증 | 8 |
| 비용 최적화·모델 비교·회귀 테스트 | 9, 13 (평가) |
| 벡터DB | 6 |

`docs/BACKLOG.md`의 "처리할 단계"는 v1의 단계 번호다. v1의 4단계(structure)는 v2의 1–4단계가, v1의 5단계(벡터DB)는 v2의 5단계가, v1의 6단계(평가·문서화)는 v2의 6단계가 대신한다.

### 12.4 v1에서 옮기지 않고 버린 것

| 버린 것 | v1 위치 | 이유 |
| --- | --- | --- |
| 원칙 "판정은 결정적 도구가, 수정안 작성은 AI가", "AI는 판정하지 않는다" | 개요, F6, 결정 기록 | 11.1 |
| "AI를 안 부르는 것이 기본" | 비용 최적화 1 | 11.1 |
| 스킬 체계: `SKILLS` 등록, `skills.enabled`, `/fix <lint\|static\|structure>`, 스킬별 수정안 브랜치 | F6, F7, F10, F11 | 규칙별 확인 방법과 수정 방법별 묶음으로 바뀜 |
| 파일 구조에서 규칙 추정: base의 import 그래프에 없던 방향, 폴더별 이름 패턴, 새 최상위 폴더 알림 | F6 "3순위" | 문서에 없는 규칙은 출처를 보여 줄 수 없다. 문서가 import 방향을 말하면 검사기로 확인한다 |
| 규칙 출처의 우선순위와 "README의 규칙은 경고로만", "설정 파일과 README가 다르면 설정 파일" | 설정 | 문서가 첫 번째 출처다. 도구 설정이 끈 규칙을 따르는 것만 남겼다 (4.3) |
| 읽어낸 규칙을 `.pipeline.yaml` 초안으로 코멘트에 제안, `structure.forbidden_imports`·`path_patterns` | F6, 설정 | 읽은 규칙은 코멘트에 보이고, 바로잡으려면 README를 고치거나 `rules.disabled`를 쓴다 |
| `skills.static.min_repeats`, `model.backend`, `notify.slack.team`, `notify.slack.ops`, `extensions.*` | 스키마 | 10절의 표 |
| 파이프라인 내장 규칙과 검사기: 정적변수 검사(`PLR2004`·`PLW0603`을 파이프라인이 골라 켜기, `STATIC-REPEAT`, `STATIC-MUTABLE`)와 그 세부 규칙, 설정이 없을 때의 ruff 기본 규칙 | F6 "`static` 검출 규칙", 설정 | 파이프라인이 정한 규칙이다. 규칙은 저장소에서만 나온다 (2.2). static은 2단계에, ruff 기본 규칙(`--isolated`)은 3단계에 없앴다(2026-11-20). ruff 설정이 없는 저장소는 문서 규칙이 ruff 코드로 이어질 때만 그 코드로 ruff를 돌린다 |
| v1 lint 고정 단계와 `/fix lint`, `skills.enabled`, `fix-lint` 수정안 브랜치, `proposaltarget`의 거절 기억 | F6, F7, F10, F11 | 도구 설정의 규칙 묶음, 기계적 수정 묶음(`/fix mechanical`, `fix-mechanical`), `records`의 거절 기록이 대신한다 (3단계) |
| 선택 확장: 수상한 테스트 탐지, 바뀐 줄 뮤테이션, AI 테스트 생성, `pytest-testmon`, `/mutate`, `/generate-tests`, 테스트 제안 PR, mutmut 스파이크 결과 | F13, 7단계 | 새 목적에 없다. 기록은 v1에 남아 있다 |
| "오버엔지니어링" 스킬을 후순위로 둔 항목 | F6 끝 | 문서에 그런 규칙이 있으면 모델 판단으로 다뤄지므로 따로 둘 이유가 없다 |
| "가벼운 모델 + 검색"을 포함한 네 조건 모델 비교를 한 번에 | 비용 최적화 | 버리지는 않고 나눴다. 모델 비교는 6단계, 검색 유무는 5단계 |
| "대상 코드가 3개 이상 모듈을 import하면 처음부터 고성능 모델" | 라우팅 규칙 | 넣은 적이 없고 근거가 없다. 평가 결과가 필요하다고 하면 그때 넣는다 |
| 이력서 문장 형식 | 측정 지표 | 설계가 아니다. 지표 표는 14절에 남겼다 |
| 팀 서버로 옮길 때의 GitHub App 토큰 방식, 다른 백엔드(API 키, Ollama) | 보안·인증, 비용 최적화 7 | 지금 범위가 아니다. Check Run 질문만 11.3에 남겼다 |

세부 규칙 중 본문에 "v1 F○와 같다"로 적은 것(커버 여부의 줄 대응, pip 실패 분류, git 출력 고정 옵션, claude CLI 스파이크 결과)은 버린 것이 아니다. v1의 그 절이 계속 기준이다.

### 12.5 2026-11-21~22에 바뀐 코드 경로

2026-11-21에 `app/` 하나를 `api/`(웹)·`worker/`(워커)·`shared/`(웹과 워커가 둘 다 import하는 것)로 나눴고(11.1), 2026-11-22에 그 셋을 다시 `app/` 아래(`app/api/`, `app/worker/`, `app/shared/`)로 옮겼다(11.1). 최상위에 있던 것은 하루 동안이다. 동작은 두 번 다 바뀌지 않았다. 날짜가 붙은 기록(이 문서 11.1과 13절의 괄호 안 이력, 12.1, `docs/HISTORY.md`(2026-11-22까지 `docs/CLAUDE.md`의 "현재 단계"에 있던 항목), `docs/JOURNAL.md`, `docs/BACKLOG.md`의 닫힌 항목)의 경로는 그때의 이름이고 고치지 않았다. 아래 표로 읽는다. 2026-11-21 전의 기록에서 `models/x.py`, `worker/x.py`, `sandbox/x.py`, `controllers/x.py`는 각각 `app/models/x.py`, `app/worker/x.py`, `app/sandbox/x.py`, `app/controllers/x.py`를 줄여 쓴 것이다. 2026-11-21~22의 기록에서 `api/x.py`, `worker/x.py`, `shared/x.py`는 최상위 경로이고 지금은 앞에 `app/`이 붙는다.

**자리를 정한 규칙.** 2026-11-21의 표는 이 규칙으로 계산했다(저장소 밖 `.audit/split-app/plan.py`). 2026-11-22에는 세 폴더 앞에 `app/`만 붙였다(`.audit/into-app/apply.py`).

- 웹 코드(`api.py`, `controllers/`, `services/`, `views/`)는 `app/api/` 한 폴더다. 합쳐 500줄 안팎이라 세 폴더를 두지 않는다. `api.py`는 `app/api/main.py`다.
- `app/shared/`에는 웹 코드와 워커 코드가 둘 다 직접 import하는 것과, 그것이 import하는 것만 둔다(import 그래프로 쟀다). 웹만 쓰는 것은 `app/api/`로 간다.
- 순수 모듈은 이름이 `_pure`로 끝나고 외부 호출이 없는 모듈이다. 옛 `app/models/`의 모듈이 모두 이것이 되었다. `_pure` 모듈은 저장소 안에서 다른 `_pure` 모듈만 import하고 금지 목록의 라이브러리를 import하지 않는다(`tests/test_layers.py`). 같은 폴더에 같은 이름의 실행 코드가 있는 것은 일부이고, 실행 코드와 이름이 다른 것은 `docs/CLAUDE.md` "코드 구조"의 표에 있다.
- 워커의 단계 실행 코드는 단계 폴더에 둔다. `app/worker/rules/`: 규칙 경로(`doc_rules.py`), 규칙 읽기(`rules.py`), 검사 방법 정하기(`checkers.py`), 위반 찾기(`detect.py`), 모델 판단(`judge.py`), PR 단위 확인(`pr_check.py`). `app/worker/fix/`: 수정 판단(`decide.py`), 기계적 수정(`mechanical_fix.py`), 모델 수정(`model_fix.py`), 출구 검사의 허용 범위(`scope.py`), 수정안 PR(`proposal.py`). 그 밖의 실행 코드(흐름, 진입점, 상태, 기록, 결과 코멘트, 검색, 평가)는 `app/worker/` 맨 위다. `sandbox/`는 `app/worker/sandbox/`다.
- 워커만 쓰는 순수 모듈은 그것을 import하는 실행 코드 중 `app/worker/` 맨 위가 아닌 것들이 있는 폴더의 가장 가까운 공통 폴더에 둔다. 한 단계만 쓰면 그 단계 폴더(`app/worker/fix/fix_decision_pure.py`), 여러 폴더가 쓰면 `app/worker/` 맨 위(`app/worker/violation_pure.py`)다. 맨 위의 흐름·코멘트 코드가 단계의 타입을 import하는 것은 자리를 정하지 않는다.
- 수동 명령은 웹 쪽(`python -m app.api.cli sync`)과 워커 쪽(`python -m app.worker.cli reindex`, `eval`)으로 나눴다. 한 파일이 웹과 워커를 같이 import하면 어느 폴더에도 둘 수 없다.
- 설정(`config.py`)이 `models/rag.py`를 import해 웹이 검색 모듈 전체를 쓰는 것으로 재어졌다. 설정이 쓰는 임베딩 모델 목록만 `app/shared/embed_models_pure.py`로 떼어 냈다(나누기 직전 커밋의 `app/models/embed_models.py`).
- 테스트는 코드의 폴더를 따르되 `app` 단계는 두지 않는다(`tests/api/`, `tests/worker/`, `tests/shared/`). 나누기 전에도 `tests/models/`처럼 `app` 단계가 없었다. 순수 모듈 `x_pure.py`의 테스트는 `test_x_pure.py`다. `tests/models/`, `tests/controllers/`, `tests/services/`, `tests/notify/`, `tests/sandbox/`는 없어졌다(`tests/notify/test_macos*.py` → `tests/shared/test_notify*.py`, `tests/test_api.py` → `tests/api/test_main.py`).
- 2026-11-22에 `app/worker/markdown.py`와 `app/worker/pr_check_report.py`를 순수 모듈 `markdown_pure.py`, `pr_check_report_pure.py`로 바꿨다.

**문자열로 남은 옛 경로.** 큐(RQ)의 작업은 처리 함수를 문자열로 저장하고(대기, 재시도 예약, 실패 목록), LangGraph 체크포인트(SQLite)는 상태 안의 객체를 모듈 경로와 클래스 이름으로 저장한다. 큐의 함수 경로는 나누기 전과 같은 `app.worker.main.handle`로 돌아왔다. 하루 동안의 코드가 넣은 `worker.main.handle` 작업만 새 코드에서 풀리지 않는다. 체크포인트의 모듈 경로는 나누기 전의 `app.models.x`도, 하루 동안의 `worker.x_pure`도 아닌 `app.worker.x_pure`(웹과 같이 쓰는 것은 `app.shared.x_pure`)가 된다. 그래서 운영 서버에 반영할 때는 재시작 전에 큐를 비우고, 멈춰 둔 thread(승인 대기, 호출 상한으로 남은 `/fix`, 전달 재시도)는 `/recheck`로 새로 시작한다.

| 옛 경로 | 지금 경로 |
| --- | --- |
| `app/api.py` | `app/api/main.py` |
| `app/cli.py` | `app/worker/cli.py` |
| `app/config.py` | `app/shared/config.py` |
| `app/controllers/sync.py` | `app/api/sync.py` |
| `app/controllers/webhook.py` | `app/api/webhook.py` |
| `app/services/github.py` | `app/api/github.py` |
| `app/services/history.py` | `app/api/history.py` |
| `app/services/queue.py` | `app/api/queue.py` |
| `app/services/signature.py` | `app/api/signature.py` |
| `app/views/responses.py` | `app/api/responses.py` |
| `app/models/access.py` | `app/worker/access_pure.py` |
| `app/models/cache_key.py` | `app/worker/cache_key_pure.py` |
| `app/models/call_budget.py` | `app/worker/call_budget_pure.py` |
| `app/models/checker.py` | `app/worker/checker_pure.py` |
| `app/models/checker_authoring.py` | `app/worker/checker_authoring_pure.py` |
| `app/models/command.py` | `app/worker/command_pure.py` |
| `app/models/comment.py` | `app/shared/comment_pure.py` |
| `app/models/coverage.py` | `app/worker/coverage_pure.py` |
| `app/models/db.py` | `app/shared/db_pure.py` |
| `app/models/diff.py` | `app/worker/diff_pure.py` |
| `app/models/doc_rules.py` | `app/worker/doc_rules_pure.py` |
| `app/models/doc_sections.py` | `app/worker/rules/doc_sections_pure.py` |
| `app/models/embed_models.py` | `app/shared/embed_models_pure.py` |
| `app/models/eval_run.py` | `app/worker/eval_run_pure.py` |
| `app/models/fix.py` | `app/worker/fix/fix_pure.py` |
| `app/models/fix_decision.py` | `app/worker/fix/fix_decision_pure.py` |
| `app/models/install.py` | `app/worker/lang/install_pure.py` |
| `app/models/job.py` | `app/shared/job_pure.py` |
| `app/models/judge.py` | `app/worker/rules/judge_pure.py` |
| `app/models/model_fix.py` | `app/worker/fix/model_fix_pure.py` |
| `app/models/ops_alert.py` | `app/shared/ops_alert_pure.py` |
| `app/models/pr_check.py` | `app/worker/rules/pr_check_pure.py` |
| `app/models/proposal.py` | `app/worker/fix/proposal_pure.py` |
| `app/models/rag.py` | `app/worker/rag_pure.py` |
| `app/models/rag_switch.py` | `app/worker/rag_switch_pure.py` |
| `app/models/records.py` | `app/worker/records_pure.py` |
| `app/models/repo_config.py` | `app/worker/repo_config_pure.py` |
| `app/models/rule.py` | `app/worker/rule_pure.py` |
| `app/models/rule_reading.py` | `app/worker/rule_reading_pure.py` |
| `app/models/scope.py` | `app/worker/fix/scope_pure.py` |
| `app/models/state_keys.py` | `app/shared/state_keys_pure.py` |
| `app/models/sync_rules.py` | `app/api/sync_rules_pure.py` |
| `app/models/test_run.py` | `app/worker/test_run_pure.py` |
| `app/models/text_edit.py` | `app/worker/text_edit_pure.py` |
| `app/models/tool_choice.py` | `app/worker/rules/tool_choice_pure.py` |
| `app/models/tools.py` | `app/worker/tools_pure.py` |
| `app/models/violation.py` | `app/worker/violation_pure.py` |
| `app/notify/macos.py` | `app/shared/notify.py` |
| `app/worker/base_config.py` | `app/worker/base_config.py` |
| `app/worker/budget.py` | `app/worker/budget.py` |
| `app/worker/checkers.py` | `app/worker/rules/checkers.py` |
| `app/worker/command.py` | `app/worker/command.py` |
| `app/worker/coverage.py` | `app/worker/coverage.py` |
| `app/worker/decide.py` | `app/worker/fix/decide.py` |
| `app/worker/deps.py` | `app/worker/deps.py` |
| `app/worker/detect.py` | `app/worker/rules/detect.py` |
| `app/worker/doc_rules.py` | `app/worker/rules/doc_rules.py` |
| `app/worker/doc_rules_report.py` | `app/worker/doc_rules_report.py` |
| `app/worker/evaluation.py` | `app/worker/evaluation.py` |
| `app/worker/github.py` | `app/worker/github.py` |
| `app/worker/graph.py` | `app/worker/graph.py` |
| `app/worker/guard.py` | `app/worker/guard.py` |
| `app/worker/judge.py` | `app/worker/rules/judge.py` |
| `app/worker/loop.py` | `app/worker/loop.py` |
| `app/worker/main.py` | `app/worker/main.py` |
| `app/worker/markdown.py` | `app/worker/markdown_pure.py` |
| `app/worker/mechanical_fix.py` | `app/worker/fix/mechanical_fix.py` |
| `app/worker/memory.py` | `app/worker/memory.py` |
| `app/worker/model_fix.py` | `app/worker/fix/model_fix.py` |
| `app/worker/pr_check.py` | `app/worker/rules/pr_check.py` |
| `app/worker/pr_check_report.py` | `app/worker/pr_check_report_pure.py` |
| `app/worker/preflight.py` | `app/worker/preflight.py` |
| `app/worker/proposal.py` | `app/worker/fix/proposal.py` |
| `app/worker/records.py` | `app/worker/records.py` |
| `app/worker/repo.py` | `app/worker/repo.py` |
| `app/worker/report.py` | `app/worker/report.py` |
| `app/worker/retry.py` | `app/worker/retry.py` |
| `app/worker/rules.py` | `app/worker/rules/rules.py` |
| `app/worker/scope.py` | `app/worker/fix/scope.py` |
| `app/worker/state.py` | `app/worker/state.py` |
| `app/worker/update.py` | `app/worker/update.py` |
| `app/worker/ai/claude_cli.py` | `app/worker/ai/claude_cli.py` |
| `app/worker/ai/path_hook.py` | `app/worker/ai/path_hook.py` |
| `app/worker/ai/workdir.py` | `app/worker/ai/workdir.py` |
| `app/worker/lang/python.py` | `app/worker/lang/python.py` |
| `app/sandbox/checker_runner.py` | `app/worker/sandbox/checker_runner.py` |
| `app/sandbox/container.py` | `app/worker/sandbox/container.py` |
| `app/sandbox/docker_cli.py` | `app/worker/sandbox/docker_cli.py` |
| `app/sandbox/safe_read.py` | `app/worker/sandbox/safe_read.py` |
| `app/sandbox/toolcache.py` | `app/worker/sandbox/toolcache.py` |

