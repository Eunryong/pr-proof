# CLAUDE.md

이 리포는 PR의 협업 규칙(컨벤션·정적변수·파일 구조) 위반을 검출하고, 규칙에 맞게 고친 수정안을 테스트로 검증해 제안 PR로 올리는 파이프라인이다. 테스트 실행은 수정안의 안전망이다. 전체 설계는 `docs/DESIGN.md`에 있고, 작업 전에 해당 섹션을 먼저 읽는다.

## 지켜야 할 원칙

- 판정(위반 여부, 통과·실패)은 결정적 도구(ruff, 정적변수·구조 검사기, pytest, coverage 등)만 낸다. AI는 수정안 작성과 README 규칙 변환만 하고, AI 호출 결과로 판정하지 않는다.
- 대상 리포의 규칙을 따른다. 특정 구조(MVC 등)를 강제하지 않는다.
- 웹(api)과 worker는 메모리를 공유하지 않는다. 넘길 값은 `app/models/job.py`의 `PRJob`으로만, 상태는 Redis(임시)·SQLite(영구)에 둔다.
- PR에서 온 코드와 AI가 쓴 코드는 Docker 샌드박스에서만 실행한다. 호스트에서 대상 리포의 pytest를 돌리지 않는다.
- 대상 리포의 설정과 규칙은 PR head가 아니라 base 브랜치에서 읽는다.
- 비밀값은 코드·환경변수에 넣지 않는다. `app/config.py`의 파일 읽기 함수로만 접근한다.
- subprocess에는 최소 환경만 넘긴다.
- 기능은 `docs/DESIGN.md`의 단계 순서대로 만들고, 각 단계는 동작하는 상태로 끝낸다. 다음 단계의 구조(플러그인 로더, Chroma, 체크포인터 등)를 미리 만들지 않는다.

## 작업 범위 판단 (Triage)

새로 발견한 문제나 개선점은 작업 전에 먼저 분류한다.

판단 질문: "지금 안 고치면, 현재 단계에서 PR에 틀린 결과가 보이거나,
작업이 멈추거나, 무한 반복되거나, 보안 구멍이 생기는가?"

- 그렇다 → **지금 고침**. 현재 작업에 포함한다.
- 아니다 → **나중**. 고치지 않고 `docs/BACKLOG.md`에 적고 넘어간다.

BACKLOG.md 형식:
- [ ] (발견 단계) 내용 — 왜 나중인지 한 줄 / 처리할 단계

작업을 마치고 보고할 때는 맨 끝에 두 목록을 따로 적는다.
- 지금 고친 것: 각각 위 질문의 어느 조건에 해당했는지
- BACKLOG에 적은 것: 새로 추가한 항목

미래 단계의 설정·옵션·확장 구조는 그 단계에 들어가기 전까지 만들지 않는다.
드문 가장자리 경우는 분류 규칙의 단위 테스트로 확인하고,
실제 Docker 재현 테스트는 PR에 결과가 잘못 보이는 경우에만 추가한다.

## 현재 단계

- 0단계 완료. 1단계 (수신 → 체크아웃 → 샌드박스 테스트 → PR 코멘트, PR 상태 동기화) 체크리스트 완료. 2단계 진행 중 (설정 읽기·lint 검출·규칙 줄 완료, 다음은 명령 처리와 `/fix lint`). 이후 2단계 lint → 3단계 static → 4단계 structure 순서다. 뮤테이션·AI 테스트 생성은 7단계 선택 확장이다. 완료하면 `docs/DESIGN.md`의 체크리스트를 갱신한다.
- 작업은 기능 단위 브랜치와 PR로 진행한다.
- 첫 대상 리포는 별도 예제 리포 `Eunryong/eval`이다. 이 리포(`pr-proof`)는 자기 PR을 검사하지 않는다.
- 실제 협업 리포 검증은 나중에 한다. 각 단계는 예제 리포와 저장해 둔 웹훅 페이로드로 검증한다.

## 실행

```
docker compose up -d       # redis
source .venv/bin/activate  # Procfile의 python·uvicorn은 venv에서 찾는다
cp .env.example .env       # 처음 한 번. API_PORT 등 로컬 실행 설정 (비밀값은 넣지 않음)
honcho start               # api(웹훅 + 동기화 루프), worker
pytest                # 파이프라인 자체 테스트 (Docker 없이)
pytest -m docker      # 실제 Docker·예제 리포가 필요한 통합 테스트
```

워커 실행 전 `gh auth login`, `claude` 로그인, Docker 실행이 필요하다. preflight가 확인한다.

## 코드 구조

- 웹(api)은 GitHub 변화를 감지해 `PRJob`을 큐에 넣기만 한다. 감지 방법은 웹훅과 동기화(api 안의 주기 루프) 두 가지다
- 워커는 큐에서 작업을 꺼내 처리하고 결과(코멘트, 제안 PR)를 GitHub에 남긴다
- 둘 사이에는 큐와 Redis·SQLite 상태만 오간다. 키 형식과 테이블 정의는 `models/`, 읽기·쓰기 코드는 각자 자기 쪽에 둔다
- `app/models/`: 웹·워커 공용 데이터. 외부 호출 없음
- `app/controllers/`, `app/services/`, `app/views/`: 웹 쪽 MVC (webhook, sync). GitHub은 조회만 한다
- `app/worker/`: 워커 진입점, 작업 흐름(LangGraph), PR 코멘트, 처리 기록 쓰기, GitHub 쓰기
- `app/sandbox/`: 워커가 쓰는 docker 도구 (`docker_cli`, `venv`, `container`, `pytest_runner`, `ruff_runner`)
- 웹 쪽은 `worker/`·`sandbox/`를, 워커는 웹 쪽을 import하지 않는다. `tests/test_layers.py`가 검사한다
- 테스트는 `tests/` 아래 같은 이름의 폴더에 둔다

## 코드 스타일

- Python 3.11+, 타입 힌트 필수, ruff로 포맷·린트
- 모듈 수준 상수(대문자 이름)는 `typing.Final`로 선언한다. dict 같은 가변 값은 `Final[Mapping[...]]`로 읽기 전용 타입을 준다. `tests/test_conventions.py`가 검사한다
- 테스트에서 바꿔야 하는 값은 상수를 monkeypatch하지 말고 함수 인자로 받는다
- 새 기능에는 단위 테스트를 같이 작성한다. 테스트는 경계값과 실패 경로를 포함한다.
