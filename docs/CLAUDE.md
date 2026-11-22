# CLAUDE.md

이 리포는 팀의 규칙을 그 저장소의 문서에서 읽고, PR이 바꾼 코드에서 어긋난 곳을 찾아, 모델이 고치고 테스트로 확인한 수정안을 별도 PR로 올리는 파이프라인이다. 사람은 수정안 PR을 머지하거나 닫기만 한다. 수정을 쓰는 모델은 검사기와 샌드박스 테스트를 도구로 불러 테스트를 쓰고 돌려 보며 고치고, 프로그램은 올리기 직전에 같은 검사를 한 번 더 돌린다(출구 검사). 전체 설계는 `docs/DESIGN.md`(v2)에 있고, 작업 전에 해당 섹션을 먼저 읽는다. `docs/DESIGN.v1.md`는 예전 설계의 보관본이고, v2가 "v1 F○와 같다"로 가리키는 세부 규칙만 거기서 읽는다.

## 지켜야 할 원칙

- 모델이 일을 쥐고, 프로그램(저장소의 도구, 검사기, 샌드박스 테스트)은 모델이 필요할 때 부르는 도구다. 프로그램이 모델 밖에서 하는 것은 올리기 직전의 출구 검사와 경계(샌드박스, 허용 범위, 비밀값, 호출·시간·크기 상한)다. 머지는 사람이 한다(DESIGN 2절, 2026-11-22). 결정은 네 종류다(DESIGN 2.1): 규칙 선택(모델), 위반 판단(도구가 있으면 도구, 없으면 모델), 수정 판단(모델), 수정 작성과 확인(모델이 도구로, 출구 검사는 프로그램). 출구 검사는 모델의 말을 믿지 않고 프로그램이 직접 돌린다.
- 모델의 결과는 입력 해시별로 저장하고 다시 쓴다. 재현성은 모델을 금지해서가 아니라 저장해서 보장한다.
- 규칙은 대상 리포의 문서와 도구 설정에서만 나온다. 파이프라인은 내장 규칙도 내장 검사기도 갖지 않는다. 특정 구조(MVC 등)를 강제하지 않는다.
- 도구는 미리 깔지 않고 규칙 계획에 필요할 때만 받아 캐시한다(DESIGN 2.3).
- 웹(api)과 worker는 메모리를 공유하지 않는다. 넘길 값은 `app/shared/job_pure.py`의 `PRJob`으로만, 상태는 Redis(임시)·SQLite(영구)에 둔다.
- PR에서 온 코드와 모델이 쓴 코드(수정안, 검사기, 만든 테스트)는 Docker 샌드박스에서만 실행한다. 호스트에서 대상 리포의 pytest를 돌리지 않는다. 모델에게 명령 실행 도구(Bash)를 주지 않는다. 모델이 부르는 도구는 파이프라인의 코드다.
- 대상 리포의 설정과 규칙은 PR head가 아니라 base 브랜치에서 읽는다.
- 비밀값은 코드·환경변수에 넣지 않는다. `app/shared/config.py`의 파일 읽기 함수로만 접근한다.
- subprocess에는 최소 환경만 넘긴다.
- 기능은 `docs/DESIGN.md`의 표에서 파일을 찾아 그 파일만 읽고, 그 단계 순서대로 만들고, 각 단계는 동작하는 상태로 끝낸다. 다음 단계의 구조(플러그인 로더, Chroma, 체크포인터 등)를 미리 만들지 않는다.

## 리뷰 규칙

리뷰와 보고의 기준은 `docs/DESIGN.md` 13절의 단계별 "완료 기준"과 8.0의 위협 정리 표다. 둘은 확정되어 있고, 단계를 진행하는 중에는 그 단계의 완료 기준을 늘리지 않는다.

**보고는 완료 기준으로 쓴다.** 작업을 마치고 보고할 때는 지금 단계의 완료 기준 중 무엇을 채웠는지를 번호(예: 1-6)로 적는다. 채운 기준마다 근거(테스트 이름, 실행 결과, PR 링크)를 붙이고, 아직 채우지 못한 기준을 따로 적는다. 기준에 없는 일을 했으면 왜 했는지 적는다.

**새로 발견한 것은 먼저 분류한다.**

- 지금 단계의 완료 기준을 깨거나 `docs/DESIGN.md` 2절의 원칙을 깨면 → **지금 처리**한다.
- 그 밖은 → 고치지 않고 `docs/BACKLOG.md`에 적고 넘어간다. 완료 기준에 항목을 더하지 않는다.
- 보안 항목은 위협 정리 표(DESIGN 8.0)의 시점을 따른다. 시점이 `지금`인 줄에 해당하면 지금 처리하고, `공개 전`이면 BACKLOG에 적고 표의 번호를 가리킨다. 표에 없는 위협이면 표에 줄을 더하고 시점을 사람에게 묻는다.
- 완료 기준이 틀렸다고 보이면 스스로 고치지 않는다. 이유를 적어 사람에게 묻고, 바꾸면 DESIGN 11.1의 결정 기록 표에 남긴다.

BACKLOG.md 형식:
- [ ] (발견 단계) 내용 — 왜 나중인지 한 줄 / 처리할 단계

보고의 맨 끝에는 두 목록을 따로 적는다.
- 지금 고친 것: 각각 어느 완료 기준이나 어느 원칙, 위협 표의 어느 줄에 해당했는지
- BACKLOG에 적은 것: 새로 추가한 항목

미래 단계의 설정·옵션·확장 구조는 그 단계에 들어가기 전까지 만들지 않는다.
드문 가장자리 경우는 분류 규칙의 단위 테스트로 확인하고,
실제 Docker 재현 테스트는 PR에 결과가 잘못 보이는 경우에만 추가한다.

## 단계 마무리 점검

단계를 끝냈다고 표시하기 전에 아래를 확인하고, 결과를 보고에 적는다.

1. 완료 기준: 이 단계의 완료 기준이 모두 채워졌다. 하나라도 남으면 단계를 끝내지 않는다.
2. 설계와 코드 일치: 이 단계에 해당하는 `docs/DESIGN.md` 내용 중 코드에 없는 것을 찾아 목록으로 적는다.
3. BACKLOG: "처리할 단계"가 이 단계인 항목을 처리하거나, 처리하지 않는 이유와 새 단계를 적는다.
4. 테스트: `pytest`와 `pytest -m docker`가 모두 통과한다.

## 설계 문서 변경 규칙

- `docs/DESIGN.md`를 바꾸면 같은 작업에서 코드도 맞춘다.
- 같은 작업에서 맞추지 못하면, 보고 끝에 "문서에만 있고 코드에 없는 것" 목록을 적는다.
- 외부 리뷰에서 나온 항목도 위 리뷰 규칙으로 분류한다. 지금 처리로 분류된 항목은 다음 기능 작업보다 먼저 처리한다.
- 주 기능이나 목표가 바뀌면 개요만 고치지 말고 핵심 원칙, 결정 기록 표, 기본값을 새 목표에 맞는지 하나씩 다시 검토한다.

## 지금의 방향과 상태

- 원칙(2026-11-22, DESIGN 2절): 모델이 일을 쥐고, 프로그램(저장소의 도구, 검사기, 샌드박스 테스트)은 모델이 필요할 때 부르는 도구다. 프로그램이 모델 밖에서 하는 것은 올리기 직전의 출구 검사와 경계(샌드박스, 허용 범위, 비밀값, 호출·시간·크기 상한)다. 머지는 사람이 한다.
- PR 하나의 순서: 작성자 확인(가드레일) → 규칙 읽기(모델, base의 문서와 도구 설정) → 규칙마다 확인 방법(저장소의 도구 / 모델이 쓴 검사기 / 모델 판단) → 위반 찾기 → 테스트 기준선 → 수정 판단(정해진 것은 프로그램, 나머지는 모델) → 수정 작성 → 출구 검사 → 수정안 PR과 결과 코멘트.
- 수정 작성(DESIGN 4.7)은 모델이 한 호출 안에서 한다: 있던 테스트가 위반 줄을 실행하지 않으면 테스트(`tests/test_pipeline_<이름>.py`)를 먼저 쓰고, 도구 `run_tests`(샌드박스)와 `check_rules`(검사기)로 돌려 보며 고치고 다시 확인한다.
- 출구 검사(DESIGN 4.8)는 프로그램이 같은 검사를 한 번 더 돌린다: 허용 범위, 비밀값, 설명 확인, 규칙 재검사, 만든 테스트의 조건(고치기 전 통과, 위반 줄 실행, 겹치지 않음, 고친 뒤 통과, 빈 테스트 아님), 테스트, 크기. 맞지 않는 만든 테스트는 그 파일만 뺀다.
- 모델이 코드를 보는 방식은 단계마다 다르다(DESIGN 8.4). 수정 작성은 저장소를 읽고 쓰는 도구와 파이프라인 도구를 받는다. 위반 찾기의 모델 판단(DESIGN 4.5)과 PR 설명 확인(DESIGN 4.5 끝)은 읽기 도구(Read, Grep, Glob)로 PR의 코드에서 필요한 곳을 찾아 읽는다(2026-11-22). 규칙 읽기, 검사기 작성, 수정 판단, PR 단위 확인은 도구 없이 프로그램이 골라 준 자료만 받는다.
- 확인은 실제 모델로 한다. 가짜 모델은 없애는 중이고(2026-11-22, DESIGN 11.1) 남은 곳은 DESIGN 12.2에 있다. 새 테스트에 가짜 모델을 만들지 않는다.
- 대상 저장소: 예제 `Eunryong/eval`, 저장소 주인이 쓰는 `Eunryong/MathLLM`(둘 다 비공개). 이 리포(`pr-proof`)는 자기 PR을 검사하지 않는다. 실제 모델 확인은 `PR_PROOF_REAL_AI=1`(CLI)과 `PR_PROOF_REAL_MODEL=1`(평가 케이스)로 켠다.
- 열려 있는 완료 기준은 `docs/DESIGN.md` 13절, 문서와 코드가 다른 곳은 12.2, 주인에게 물은 것은 11.3에 있다. 코드 주석의 "DESIGN.md F7" 같은 번호는 v1의 절이고 12.3으로 읽는다.
- 지나간 작업의 기록은 `docs/HISTORY.md`와 `docs/JOURNAL.md`에 있다. 지금의 지시가 아니다.
- 작업은 기능 단위 브랜치와 PR로 진행한다.
- 이 절에 날짜가 붙은 작업 이력을 다시 쌓지 않는다. 이력은 `docs/HISTORY.md`·`docs/JOURNAL.md`, 결정은 DESIGN 11.1에 적고, 이 절은 지금의 방향이 바뀔 때만 고친다.

## 실행

```
docker compose up -d       # redis
source .venv/bin/activate  # Procfile의 python·uvicorn은 venv에서 찾는다
cp .env.example .env       # 처음 한 번. API_PORT 등 로컬 실행 설정 (비밀값은 넣지 않음)
honcho start               # api(웹훅 + 동기화 루프), worker
scripts/start.sh           # venv를 켜지 않은 셸이나 launchd에서. .venv/bin과 gh·claude·docker 경로를 정하고 honcho start
scripts/install-launchd.sh # 사용자 LaunchAgent로 등록 (죽으면 다시 띄움). 상태·로그·제거는 README "launchd로 띄워 두기"
pytest                # 파이프라인 자체 테스트 (Docker 없이)
pytest -m docker      # 실제 Docker·예제 리포가 필요한 통합 테스트 (이미지 빌드와 Python 런타임·ast-grep·ruff 받기에 네트워크가 필요하다)
pip install -e '.[rag]'   # 과거 기록 검색을 쓸 때만(rag.enabled: on, 또는 auto가 켤 때). chromadb·onnxruntime·tokenizers·numpy
python -m app.worker.cli reindex # 벡터 저장소(data_dir/chroma/)를 SQLite의 기록으로 다시 만든다
python -m app.worker.cli eval --baseline --db <경로>  # 평가 세트 전부를 돌려 eval_runs에 기준선으로 남긴다. --baseline 없이 돌리면 기준선과 비교 (--real-model은 claude를 부른다. --out <폴더>는 junit·호출 기록을 남기고 그 폴더의 memo.jsonl을 다시 쓴다)
PR_PROOF_REAL_EMBED=1 PR_PROOF_EMBED_DIR=<모델 폴더> pytest tests/worker/test_real_embed.py  # 실제 임베딩 모델의 recall@k
```

워커 실행 전 `gh auth login`, `claude` 로그인, Docker 실행이 필요하다. preflight가 확인한다. 워커는 preflight 전에 DB 스키마(새 테이블, NULL 허용 열)를 맞추고, 맞추지 못하면 1로 끝난다.

## 코드 구조

패키지는 `app` 하나다. 2026-11-21~22의 옛 경로(나누기 전의 `app/models/` 등, 하루 동안의 최상위 `api/`·`worker/`·`shared/`)는 `docs/DESIGN.md` 12.5의 대응표로 읽는다.

```
app/
  api/          웹. GitHub 변화를 감지해 PRJob을 큐에 넣기만 한다
  worker/       워커. 큐에서 작업을 꺼내 처리하고 결과(코멘트, 수정안 PR)를 GitHub에 남긴다
    rules/      규칙 단계: 규칙 읽기, 검사 방법 정하기, 위반 찾기, 모델 판단, PR 단위 확인
    fix/        수정 단계: 수정 판단, 기계적 수정, 모델 수정과 출구 검사, 수정안 PR
    sandbox/    언어와 무관한 docker 도구
    ai/         claude CLI 호출과 수정 작성의 도구 서버
    lang/       Python 언어 프로필
  shared/       웹과 워커 사이의 계약 (둘 다 import하는 것만)
tests/          코드와 같은 이름의 폴더 (tests/api, tests/worker, tests/shared. app 단계는 두지 않는다)
```

**웹 (`app/api/`).** FastAPI 앱(`main.py`), 웹훅(`webhook.py`), 동기화(`sync.py`, api 안의 주기 루프), GitHub 조회(`github.py`), 큐 등록(`queue.py`), 처리 기록 읽기(`history.py`), 서명 확인(`signature.py`), 응답(`responses.py`), 수동 동기화(`cli.py`). GitHub은 조회만 한다.

**워커 (`app/worker/`).**

- 맨 위: 진입점(`main.py`), 작업 흐름(`graph.py`, LangGraph), 작업 종류(`update.py`, `command.py`), 가드레일(`guard.py`), 체크아웃(`repo.py`), base 설정(`base_config.py`: `.pipeline.yaml`, ruff 설정과 버전), 상태와 처리 기록 쓰기(`state.py`), GitHub 쓰기(`github.py`), 결과 코멘트(`report.py`, `doc_rules_report.py`, `pr_check_report_pure.py`, `markdown_pure.py`), 고친 위반의 테스트 표시(`test_lines.py`), 공용 서브그래프 틀(`loop.py`), 호출 상한(`budget.py`), 기록(`records.py`), 검색(`memory.py`: 임베딩, Chroma 저장과 top-k, 밀린 기록 임베딩, reindex, 큰 규칙 문서의 조각 검색), 평가(`evaluation.py`), 수동 명령(`cli.py`: reindex, eval). 여러 단계가 같이 쓰는 순수 모듈도 여기 있다
- `rules/`: 규칙 경로(`doc_rules.py`) → 규칙 읽기(`rules.py`) → 검사 방법 정하기(`checkers.py`: 도구 설정의 규칙 묶음, 문서 규칙의 도구 선택, 검사기 작성) → 위반 찾기(`detect.py`) · 모델의 직접 판단(`judge.py`) · PR 단위 확인(`pr_check.py`, 알리기만)
- `fix/`: 수정 판단(`decide.py`) → 기계적 수정(`mechanical_fix.py`) · 모델 수정(`model_fix.py`: 수정 작성과 도구 둘의 구현, 출구 검사) · 허용 범위(`scope.py`) → 수정안 PR(`proposal.py`). 만든 테스트의 판정은 `app/worker/generated_tests_pure.py`, 만든 테스트의 자리는 `scope_pure.is_generated_test`
- `sandbox/`: `docker_cli`, `container`, `toolcache`, `checker_runner`, `safe_read`. 도구(ast-grep, ruff)와 Python 런타임은 `toolcache.py`가 고정한 버전과 SHA256으로 받아 캐시한다. 샌드박스 이미지에는 언어가 없다 (`debian:bookworm-slim`에 curl과 인증서)
- `ai/`: claude CLI 호출(`claude_cli.py`, 모양 넷: 수정 작성의 `file_tools`·`fix_tools`, 읽기만 하는 `read_tools`, 도구 없는 `no_tools`), 모델 전용 폴더(`workdir.py`), 경로 감시 훅(`path_hook.py`), 수정 작성의 파이프라인 도구 서버(`tool_server.py`)와 CLI가 띄우는 중계(`mcp_relay.py`). MCP 메시지와 도구 상한은 `app/worker/mcp_pure.py`
- `lang/python.py`: Python 언어 프로필 (런타임 준비, `.venv` 설치, pytest 실행과 결과 읽기, 위반 줄의 커버리지 읽기, ruff). 테스트 파일의 정의(`fix/scope_pure.py`), coverage.xml 파싱(`coverage_pure.py`), ruff 설정 읽기와 도구 목록(`tools_pure.py`)은 순수 모듈에 있다. 두 번째 언어와 프로필 인터페이스는 만들지 않는다

**웹과 워커 사이의 계약 (`app/shared/`).** 웹과 워커는 메모리를 공유하지 않는다. 둘 사이에는 큐와 Redis·SQLite 상태만 오가고, 큐에 넣는 값은 작업 계약(`job_pure.py`의 `PRJob`)뿐이다. 키 형식과 큐 이름(`state_keys_pure.py`), SQLite 테이블 정의(`db_pure.py`)는 여기 두고, 읽기·쓰기 코드는 각자 자기 쪽에 둔다. 그 밖에 코멘트 표시(`comment_pure.py`), 운영 알림의 종류와 글(`ops_alert_pure.py`), 임베딩 모델 목록(`embed_models_pure.py`), 설정과 비밀값 읽기(`config.py`), macOS 알림(`notify.py`)이 있다.

**순수 모듈.** 이름이 `_pure.py`로 끝나는 모듈은 바깥에 손대지 않는다: 파일, 프로세스, 네트워크, Redis, DB 연결을 쓰지 않고 받은 값으로 계산만 한다(데이터 정의, 판정, 프롬프트 만들기, 답 읽기). 대부분은 나누기 전의 `app/models/`에 있던 모듈이다. 순수 모듈은 그것을 import하는 실행 코드 중 `app/worker/` 맨 위가 아닌 것들의 가장 가까운 공통 폴더에 둔다: 한 단계만 쓰면 그 단계 폴더(`app/worker/fix/fix_decision_pure.py`), 여러 폴더가 쓰면 `app/worker/` 맨 위(`app/worker/violation_pure.py`), 웹과 워커가 같이 쓰면 `app/shared/`, 웹만 쓰면 `app/api/`.

같은 폴더에 같은 이름의 실행 코드 `x.py`가 있는 순수 모듈은 여덟 개다: `command_pure`, `coverage_pure`, `records_pure`, `fix/model_fix_pure`, `fix/proposal_pure`, `fix/scope_pure`, `rules/judge_pure`, `rules/pr_check_pure`. 아래는 주로 부르는 실행 코드와 이름이 다른 것이다.

| 실행 코드 | 순수 모듈 |
| --- | --- |
| `app/worker/fix/decide.py` | `app/worker/fix/fix_decision_pure.py` |
| `app/worker/budget.py` | `app/worker/call_budget_pure.py` |
| `app/worker/memory.py` | `app/worker/rag_pure.py`, `app/worker/rag_switch_pure.py` |
| `app/worker/evaluation.py` | `app/worker/eval_run_pure.py` |
| `app/worker/rules/rules.py` | `app/worker/rule_reading_pure.py`, `app/worker/rules/doc_sections_pure.py` |
| `app/worker/rules/checkers.py` | `app/worker/checker_authoring_pure.py`, `app/worker/rules/tool_choice_pure.py` |
| `app/worker/lang/python.py` | `app/worker/lang/install_pure.py` |
| `app/api/sync.py` | `app/api/sync_rules_pure.py` |

나머지(`rule_pure`, `violation_pure`, `doc_rules_pure`, `checker_pure`, `diff_pure`, `test_run_pure`, `shared/`의 `_pure` 등)는 여러 단계가 같이 쓰는 데이터 정의와 계산이라 짝이 없다.

**계층.** `app/api/`는 `app/worker/`를, `app/worker/`는 `app/api/`를 import하지 않고, `app/shared/`는 둘 다 import하지 않는다. `app/worker/sandbox/`는 sandbox와 순수 모듈만 import한다. 순수 모듈은 저장소 안에서 다른 순수 모듈만 import하고, 금지 목록의 라이브러리(파일 시스템, 프로세스, 네트워크, Redis, 큐, DB, 웹, 그래프, 벡터 저장소, 로그, 시계, 난수, 동적 import)와 내장 `open`을 쓰지 않는다. `tests/test_layers.py`가 import 문과 `open(` 호출을 읽어 검사한다.

**테스트.** 테스트는 `tests/` 아래 코드와 같은 이름의 폴더에 두고 `app` 단계는 두지 않는다(`app/worker/fix/decide.py` → `tests/worker/fix/test_decide.py`). 순수 모듈 `x_pure.py`의 테스트는 `test_x_pure.py`다.

## 코드 스타일

- Python 3.11+, 타입 힌트 필수, ruff로 포맷·린트
- 모듈 수준 상수(대문자 이름)는 `typing.Final`로 선언한다. dict 같은 가변 값은 `Final[Mapping[...]]`로 읽기 전용 타입을 준다. `tests/test_conventions.py`가 검사한다
- 테스트에서 바꿔야 하는 값은 상수를 monkeypatch하지 말고 함수 인자로 받는다
- 새 기능에는 단위 테스트를 같이 작성한다. 테스트는 경계값과 실패 경로를 포함한다.
