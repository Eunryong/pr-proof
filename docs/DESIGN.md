# PR 협업 규칙 검증·수정 파이프라인 설계 문서

2026-11-17 작성, 같은 날 개정 (범위 확대, 1단계부터 Docker, PR 상태 동기화, Chroma, 구조 규칙을 리포에서 읽기). 개정 배경은 루트의 `README.md`에 있다.
2026-11-17 2차 개정: 메인 기능을 테스트 품질 판정에서 **협업 규칙에 맞춘 수정**으로 바꿨다. 테스트 실행은 수정안의 안전망으로 남기고, 뮤테이션·AI 테스트 생성은 선택 확장(7단계)으로 내렸다. 기본 테스트 자동화는 CI의 영역이기 때문이다.

## 개요

PR이 올라오면 **그 팀의 협업 규칙(컨벤션, 무차별 정적변수, 파일 구조)에 어긋난 곳을 찾아, 규칙에 맞게 고친 수정안을 별도 PR로 제안**하는 로컬 파이프라인이다. 규칙은 파이프라인이 정하지 않고 대상 리포의 설정 파일, README, 실제 파일 구조에서 읽는다. 수정안은 샌드박스에서 테스트를 다시 돌려 동작을 깨지 않은 것만 올린다.

**구성 한 줄 요약:** GitHub 웹훅 + PR 상태 동기화 → **FastAPI**(수신·서명 검증·큐 등록) → Redis 큐 → 워커(LangGraph로 단계 구성) → 결정적 검출(ruff, 정적변수·구조 검사기)과 수정(ruff, claude CLI) → Docker 샌드박스에서 테스트로 검증 → PR 코멘트·제안 PR (gh CLI)

**문제의식**

- 팀마다 컨벤션과 구조 규칙이 있지만, 리뷰에서 매번 지적하고 고치는 데 시간이 든다. 리뷰가 한 사람에게 몰리면 규칙 확인은 뒤로 밀린다.
- 규칙이 README에만 적혀 있거나 아예 적혀 있지 않고 기존 코드에만 남아 있어, 새로 합류한 사람과 AI 에이전트가 지키기 어렵다.
- 하드코딩된 값과 전역 상태는 린터가 일부만 잡고, 잡아도 "어디로 옮길지"는 사람이 정해야 한다.
- 테스트 실행 자체는 CI가 하지만, 자동으로 만든 수정안이 동작을 깨지 않는지는 PR로 올리기 전에 확인해야 한다.

**목표**

1. PR에서 바뀐 코드의 컨벤션, 무차별 정적변수, 파일 구조 위반을 대상 리포의 규칙으로 검출해 PR에 공유한다. 규칙의 출처를 함께 적는다.
2. 위반을 규칙에 맞게 고친 수정안을 만든다. 컨벤션은 도구로, 정적변수와 구조는 AI로 고친다.
3. 수정안은 샌드박스에서 테스트를 통과하고, 원래 위반이 사라지고, 새 위반이 없는 것만 별도 PR로 제안한다.
4. 테스트가 지나가지 않는 코드는 자동으로 고치지 않고 알리기만 한다. 테스트가 안전망 역할을 못 하기 때문이다.

**핵심 원칙**

- **규칙은 대상 리포에서 읽는다.** 특정 패턴(MVC 등)을 강제하지 않는다. `.pipeline.yaml` → 도구 설정 → README → base 브랜치의 실제 구조 순으로 읽고, 출처를 남긴다.
- **판정은 결정적 도구가, 수정안 작성은 AI가 한다.** 위반 여부와 통과·실패는 ruff, 정적변수·구조 검사기, pytest 같은 결정적 도구(같은 입력이면 항상 같은 결과)가 낸다. AI는 수정안 작성과 README 규칙 변환만 맡는다.
- **AI가 만든 수정안도 같은 기준으로 검증한다.** 테스트를 깨뜨리거나, 위반을 남기거나, 새 위반을 만든 수정안은 제안하지 않는다.
- **테스트는 수정의 안전망이다.** 테스트 실행은 판정 대상이 아니라 수정안 검증 수단이다. 테스트가 지나가지 않는 줄은 "수정 후에도 통과"가 안전을 보장하지 않으므로 자동 수정에서 뺀다.
- **검증은 자동화하고, 머지는 사람이 결정한다.** 수정은 작성자 브랜치에 직접 커밋하지 않고 제안 PR로 올린다.
- **웹훅은 신호이고, 기준은 GitHub의 PR 상태다.** 로컬 서버는 꺼져 있을 수 있다. 놓친 웹훅은 PR 상태 동기화로 보충한다.
- **사람이 확인하지 않은 코드는 호스트에서 실행하지 않는다.** PR 코드와 AI가 고친 코드는 모두 Docker 샌드박스에서만 실행한다.

**범위**

- 포함: GitHub PR 이벤트(opened, reopened, synchronize, ready_for_review, closed)와 PR 코멘트 명령, Python 리포(pytest), 컨벤션·정적변수·파일 구조의 검출과 수정 제안, 수정안 검증을 위한 샌드박스 테스트 실행
- 선택 확장(7단계): 변경 라인 뮤테이션, 수상한 테스트 탐지, AI 테스트 생성
- 제외: CI를 대신하는 테스트 자동화, 커밋 단위 감지와 요약, 로컬 post-commit hook, AI가 판정하는 코드 리뷰, 자동 머지

## 전체 아키텍처

로컬 프로세스는 api(웹)와 worker 두 개다. api는 GitHub 변화를 감지해 작업을 큐에 넣고(웹훅 수신 + 동기화), worker는 작업을 처리해 결과를 GitHub에 남긴다. 컨테이너는 Redis, Chroma, 테스트 샌드박스 세 가지다.

```
GitHub ──웹훅──▶ api (FastAPI) ──등록──▶ Redis 큐 ──꺼내기──▶ worker (LangGraph)
  │ ▲             동기화 루프                                   │
  │ │            (시작 시 + 5분마다)                             ├─ 실행 ─▶ 테스트 샌드박스
  │ │             │                                            │         (작업마다 새 컨테이너, 네트워크 차단)
  └─┼─ PR 상태 ───┘ (gh로 조회)                                  ├─ 검색 ─▶ Chroma (과거 기록)
    │                                                          ├─ 저장 ─▶ SQLite (이력·체크포인트)
    └────────── 코멘트 · 제안 PR (gh) ◀─────────────────────────┤
                                                               └─ 운영 오류 ─▶ Slack
```

워커가 로컬에 있어 이미 로그인된 claude와 gh를 그대로 쓴다. 대신 PR에서 온 코드는 1단계부터 네트워크가 막힌 샌드박스에서만 실행한다.

| 구성 | 위치 | 역할 |
| --- | --- | --- |
| api (FastAPI) | 로컬 | GitHub 변화 감지와 작업 등록. 웹훅 수신(서명 검증, 중복 제거)과, 같은 프로세스 안의 동기화 루프(GitHub PR 상태 ↔ 처리 이력 비교) |
| worker (LangGraph) | 로컬 | 작업 흐름, 모델 라우팅, interrupt·체크포인트 |
| claude CLI | 로컬 | 정적변수·구조 수정안 작성, README 규칙 변환 (파일 읽기·쓰기만) |
| gh CLI | 로컬 | PR 정보, 코멘트, 제안 PR |
| git worktree | 로컬 | PR별 코드 폴더 |
| SQLite | 로컬 파일 | 이력, 캐시, 지표, 기록 원본, LangGraph 체크포인트 |
| Redis | 컨테이너 (compose) | 큐, 중복 방지 키, PR별 최신 커밋 |
| Chroma | 컨테이너 (compose) | 과거 수정 기록의 벡터 검색 (5단계부터) |
| 테스트 샌드박스 | 컨테이너 (작업마다) | pytest·ruff 실행 (확장: mutmut) |

## PR 처리 흐름

PR 하나는 체크아웃 → 테스트 실행(기준선) → 위반 검출 → PR 코멘트를 거친다. 위반이 있고 안전망 조건을 충족하면 리뷰어 승인(`/fix`)을 받아 수정 → 검증 → 제안 PR로 넘어간다.

```
웹훅 · 동기화 ──▶ 체크아웃 ──▶ 테스트 실행 ──▶ 위반 검출 ──────────▶ ◆ 결과 ──▶ PR 코멘트
                            (샌드박스,      (lint·static·structure,      │
                             기준선)         규칙은 base에서)              │ 위반 있음 + 안전망 충족
                                                                         ▼
                                                           수정 제안 · 대기 ──▶ /fix <skill> ──┐
                                                           (interrupt)                        │
                     ┌────────────────────────────────────────────────────────────────────────┘
                     ▼
     수정 (lint: ruff / static·structure: AI) ──▶ 결정적 검증 ──▶ 제안 PR ──▶ 리뷰어 머지 ──▶ 새 커밋으로 재검사
       ▲                                         │ 실패            (테스트 통과,
       └──────────────── 최대 3회 ─────────────────┘                 위반 해소, 새 위반 없음)
```

사람은 승인할 때(`/fix`)와 제안 PR을 머지할 때 개입한다. 그 사이 그래프는 interrupt로 멈춰 있고 상태는 SQLite에 저장된다.

테스트가 실패하거나 실행되지 않는 PR에는 수정을 제안하지 않는다. 안전망이 없기 때문이다 (F8).

**작업의 종류**

작업은 "이벤트를 처리하라"가 아니라 "PR을 현재 상태에 맞춰라"다. 워커는 PR의 현재 상태와 DB를 보고 필요한 일을 정한다. 웹훅으로 온 작업과 동기화로 온 작업이 같은 경로를 탄다.

| 작업 | 만드는 웹훅 | 워커가 하는 일 |
| --- | --- | --- |
| `update` | `pull_request` opened, reopened, synchronize, ready_for_review | 현재 head에 검사 이력이 없으면 테스트 + 위반 검출 + 결과 코멘트. head가 제안 PR의 머지 커밋이면 그 제안과 같은 종류의 제안은 다시 만들지 않음 (F10) |
| `command` | `issue_comment` created | `/recheck`, `/fix <skill>` (확장: `/mutate`, `/generate-tests`) |
| `close` | `pull_request` closed | merged면 기록 확정, 남은 제안 PR 정리 |

`pipeline/*` 브랜치에서 온 PR은 무시한다.

**작업 하나의 단계 (표시: 로컬 / 샌드박스)**

1. preflight 상태 확인 [로컬]
2. bare 캐시에서 fetch 후 worktree 생성, `changes.diff` 생성 [로컬]
3. `.venv` 확인(없거나 의존성이 바뀌었으면 설치), 컨테이너 시작 [로컬 docker]
4. worktree에서 `.venv` + pytest 실행, 결과 JSON과 커버리지 파일 생성 (수정 전 기준선) [샌드박스]
5. 규칙 읽기(base 브랜치), 바뀐 줄의 컨벤션·정적변수·구조 위반 검출, 위반 줄이 테스트로 실행되는지 확인 [PR 코드를 실행하는 도구는 샌드박스, 정적 분석과 diff는 로컬]
6. 결과 후 PR 코멘트 갱신 [로컬]
7. 위반이 있고 안전망 조건을 충족하면 수정 제안 → interrupt [로컬]
8. 승인 후 수정 [lint는 샌드박스의 ruff, static·structure는 로컬 claude] → 검증 [샌드박스], 최대 3회
9. 조건을 충족하면 제안 PR 생성 [로컬 gh]
10. 컨테이너·worktree 정리

각 단계를 시작하기 전에 자기 커밋이 아직 PR의 최신 커밋인지 확인하고, 아니면 정리하고 끝낸다 (F3).

## 기능 명세

F1–F5는 수신과 실행 기반(1단계), F6–F10은 검출·안전망·수정·제안(2–4단계), F11–F12는 그 과정에서 함께 붙는다. F13은 선택 확장이다.

### F1. 웹훅 수신 (FastAPI)

- `POST /webhook/github` 하나로 모든 이벤트를 받는다.
- `X-Hub-Signature-256` 헤더로 HMAC 서명을 검증하고, 실패하면 401을 돌려준다.
- `X-GitHub-Delivery` ID를 Redis에 저장해 재전송된 웹훅을 건너뛴다.
- 허용 목록에 있는 리포의 이벤트만 받는다. 목록은 파이프라인 쪽 설정 파일에 둔다.
- 처리 대상 이벤트만 큐에 넣고 즉시 202를 응답한다. GitHub은 10초 안에 응답해야 한다. api는 GitHub을 조회하지 않는다.
- `issue_comment`는 `action`이 `created`이고, 파이프라인 자신의 코멘트가 아니며, 작성자의 `author_association`이 OWNER·MEMBER·COLLABORATOR인 것만 받는다. 명령은 비용과 시간이 들기 때문이다.
- 무시할 이벤트: `pipeline/*` 브랜치에서 온 PR.

### F2. PR 상태 동기화

로컬 서버가 꺼져 있던 동안의 웹훅은 사라지고, GitHub은 실패한 웹훅을 자동으로 다시 보내지 않는다. api 프로세스 안의 동기화 루프가 GitHub의 PR 상태(`gh`)와 SQLite의 처리 이력을 비교해 차이를 작업으로 등록한다. 웹훅(GitHub이 알려줌)과 동기화(우리가 물어봄)는 감지 방법만 다르고, 둘 다 `PRJob`을 큐에 넣는 것으로 끝난다. 워커는 작업이 어디서 왔는지 몰라도 된다.

| 비교 | 차이가 있으면 |
| --- | --- |
| 열린 PR의 head SHA ↔ 그 SHA로 끝난 검사 이력 | `update` 등록 |
| DB에는 열림인데 GitHub에서는 닫힌 PR | `close` 등록 |
| PR 코멘트의 명령 ↔ 처리한 코멘트 ID | `command` 등록 |
| "실행 중"으로 남았는데 워커 생존 확인이 끊긴 작업 | 다시 등록 |

- 실행 시점: api 시작 시 1회, 이후 5분마다(설정값). FastAPI lifespan에서 백그라운드 루프로 띄우고, `gh` 호출은 스레드에서 실행해 웹훅 응답을 막지 않는다.
- 수동 실행은 CLI(`python -m app.cli sync`)로 한다. api는 웹훅용 터널로 외부에 열려 있으므로 HTTP 엔드포인트로 만들지 않는다.
- 서버가 꺼진 동안 커밋이 여러 개 쌓였어도 최신 head만 검사한다.
- 놓친 명령은 현재 head가 올라온 뒤에 달린 것만 처리한다. 이전 커밋에 대한 `/fix`를 뒤늦게 실행하지 않는다. F1의 작성자 권한 확인을 똑같이 적용한다.
- 동기화로 처리한 검사는 PR 코멘트에 한 줄 남긴다. 예: "동기화로 처리됨 (웹훅 누락)".
- SQLite가 기준이므로 Redis 큐가 비워져도 다음 동기화가 다시 채운다.

### F3. 큐와 워커

- 큐는 Redis + RQ를 쓴다. 워커가 하는 일이 대부분 subprocess 실행이라 동기식이 맞다.
- api(웹훅·동기화)는 작업 데이터(`PRJob`)만 큐에 넣는다.
- 중복 방지: 같은 `(repo, PR 번호, head SHA, 작업 종류)`의 작업이 대기 중이거나 실행 중이면 등록하지 않는다. 명령은 코멘트 ID로 구분한다. 이미 끝난 일인지는 워커가 DB를 보고 판단한다.
- 워커는 별도 프로세스로 로컬에서 실행하고, 동시 작업 수는 1개로 시작한다.
- 같은 PR에 새 커밋이 오면 최신 커밋만 처리한다. Redis에 PR별 최신 head SHA를 두고, 워커가 각 단계 시작 전에 확인해 자기 커밋이 낡았으면 컨테이너와 worktree를 정리하고 끝낸다. 오래 걸리는 실행(pytest, mutmut) 도중에도 확인한다. `run_in_sandbox()`가 `docker exec`를 `subprocess.Popen`으로 띄우고, 끝날 때까지 10초마다 Redis의 최신 head를 확인해 낡았으면 `docker rm -f`로 컨테이너를 지우고 정리한다. 취소는 워커 자신이 하며, 작업을 밖에서 강제 종료하지 않는다.
- 워커는 실행 중 주기적으로 생존 확인 시각을 `runs`에 남긴다.
- 프로세스들은 메모리를 공유하지 않는다. 넘길 값은 작업 데이터에, 상태는 Redis(임시)와 SQLite(영구)에 둔다.

### F4. PR 코드 준비

- 리포마다 처음 한 번만 `git clone --bare`로 코드를 캐시한다.
- PR마다 `git fetch origin pull/<n>/head` 후 `git worktree add`로 별도 폴더를 만든다.
- **git 명령은 호스트에서만 실행한다.** worktree의 `.git`은 호스트의 bare 저장소 경로를 가리키는 파일이라, `/work`만 마운트된 컨테이너 안에서는 git이 동작하지 않는다. 워커가 호스트에서 `git diff base...head`를 만들어 `.pipeline-out/changes.diff`로 저장하고, diff가 필요한 도구(바뀐 줄의 위반 검출, diff-cover, 확장의 바뀐 줄 뮤테이션)는 이 파일을 입력으로 받는다.
- 작업이 끝나면 worktree를 지운다. 인증은 `gh auth setup-git`으로 gh 로그인을 재사용한다.
- interrupt로 멈췄다가 재개할 때는 worktree를 다시 만든다. 그래프 상태에는 경로가 아니라 데이터(위반 목록, 커버 여부, 테스트 결과)만 넣는다.

### F5. 테스트 실행 (샌드박스)

- **테스트는 파이프라인이 직접 실행한다.** 기본 테스트 자동화는 CI의 몫이지만, 파이프라인이 만든 수정안은 PR로 올리기 전에 검증해야 하므로 CI 결과로 대신할 수 없다. 워커가 PR worktree에서 `.venv` + pytest로 테스트를 돌려 결과 JSON과 커버리지 파일을 남긴다. 수정 전 실행은 기준선(실패하면 수정을 제안하지 않음)과 안전망 확인(F8)에, 수정 후 실행은 수정안 검증(F7)에 쓴다.
- **실행 위치는 샌드박스 컨테이너다 (1단계부터).** worktree를 컨테이너의 `/work`에 마운트하고, 워커가 `docker exec`로 `/work/.venv/bin/pytest`를 실행한다. 명령과 산출물은 호스트에서 돌릴 때와 같고, PR 코드가 호스트 계정에서 실행되지 않는다는 점만 다르다.
- **`.venv` 준비:** 의존성 파일(`requirements*.txt`, `pyproject.toml`, lock 파일)의 해시별로 `.venv`를 만들어 호스트 캐시에 두고 `/work/.venv`로 마운트한다. 해시가 같으면 다시 설치하지 않는다. 설치는 네트워크가 열린 별도 컨테이너에서, 테스트 실행은 네트워크가 막힌 컨테이너에서 한다.
- **캐시 키:** 의존성 파일 해시 + 베이스 이미지 digest + Python 버전 (+ 검사 도구 버전). `.venv`는 인터프리터 경로에 묶여 있어 이미지가 바뀌면 다시 만들어야 한다.
- **1단계 구현:** 의존성 파일은 루트의 `requirements*.txt`와 `pyproject.toml`(`dependencies`와 모든 `optional-dependencies`, 프로젝트 자신은 제외)이다. lock 파일은 아직 쓰지 않는다. 검사 도구(`pytest-cov`, `pytest-json-report`)는 리포 의존성과 같은 `.venv`에 설치하고, pytest 버전은 리포가 고정한 것을 따른다. 설치 컨테이너에는 의존성 파일만 마운트하고, 임시 폴더에 만든 뒤 완료 표시와 함께 캐시 경로로 옮겨 설치 중 실패한 `.venv`가 캐시에 남지 않게 한다.
- **`.venv`는 테스트 컨테이너에 읽기 전용(`:ro`)으로 마운트한다.** 여러 PR이 같은 `.venv`를 쓰므로, 쓰기가 가능하면 한 PR이 site-packages의 pytest를 바꿔 다른 PR과 AI 테스트 검증 결과를 조작할 수 있다. 쓰기 가능한 마운트는 설치 컨테이너에만 준다.
- **`.venv`에는 의존성만 설치하고 대상 리포 자체는 설치하지 않는다.** 리포를 설치하면 같은 해시의 다른 PR이 남의 소스를 import한다. 테스트가 리포를 import할 수 있도록 실행할 때 `PYTHONPATH=/work`(src 구조면 `/work/src`)를 넘긴다.
- **산출물:** `pytest --json-report`의 JSON과 커버리지 파일(`.coverage`, `coverage.xml`)을 worktree의 `.pipeline-out/`에 남긴다. `.venv`와 `.pipeline-out/`은 "허용 범위 밖 변경" 검사에서 제외한다.
- 기본은 전체 테스트 실행이다. 영향받는 테스트만 실행하는 최적화(`pytest-testmon`)는 7단계에서 넣는다. PR마다 새 worktree라 기준 데이터를 base 커밋 단위로 캐시해야 하고, coverage와 함께 쓸 수 있는지 확인이 필요하다.
- 실행 함수는 `run_in_sandbox()` 하나로 묶는다. pytest, mutmut, ruff가 모두 이 함수를 거친다.
- 실패한 테스트는 한 번 재실행한다. 재실행에서 통과하면 flaky로 표시한다 (F8).

### F6. 위반 검출 (컨벤션 · 정적변수 · 파일 구조)

이 파이프라인의 메인 기능이다. 검출은 결정적 도구가 하고, PR에서 바뀐 줄(`changes.diff`)의 위반만 다룬다. 기존 코드의 위반은 PR 작성자의 책임이 아니므로 건드리지 않는다.

| 스킬 | 검출 (결정적) | 규칙의 출처 |
| --- | --- | --- |
| `lint` 컨벤션 | ruff | 대상 리포의 ruff 설정 (`pyproject.toml`, `ruff.toml`), 없으면 기본 규칙 |
| `static` 정적변수 | ruff 규칙(매직 값, `global` 사용) + 반복 리터럴·모듈 수준 가변 상태 규칙(semgrep 또는 AST 검사) | `.pipeline.yaml`, 기본 규칙 |
| `structure` 파일 구조 | 대상 리포에서 읽은 구조 규칙으로 import 방향과 파일 위치·이름 검사 | `.pipeline.yaml` → README·CONTRIBUTING → base 브랜치의 파일 구조 |

"무차별 정적변수"는 하드코딩된 리터럴(매직 넘버, 반복되는 문자열)과 남용된 전역·모듈 수준 가변 상태를 모두 가리킨다.

**구조 규칙은 대상 리포에서 읽는다.** MVC 같은 특정 패턴을 강제하지 않는다. 대상 리포의 README와 실제 파일 구조가 기준이다.

| 순위 | 출처 | 얻는 규칙 | 방법 |
| --- | --- | --- | --- |
| 1 | `.pipeline.yaml` | 명시된 레이어·경로 | 그대로 사용 |
| 2 | README·CONTRIBUTING | "controller는 repository를 직접 부르지 않는다" 같은 서술 | AI가 읽어 규칙 형식으로 변환, README가 바뀔 때만 |
| 3 | base 브랜치의 파일 구조 | 실제로 지켜지고 있는 구조 | 결정적 분석 |

- **3순위의 내용.** import 방향: base 브랜치의 패키지 간 import 그래프를 만들고, 기존 코드에 한 번도 없던 방향의 import를 PR이 처음 추가하면 경고한다. 파일 위치와 이름: 폴더별 기존 파일의 이름 패턴과 다른 파일, 새 최상위 폴더를 알린다.
- **AI는 판정하지 않는다.** AI는 README를 규칙 형식(금지 import 쌍, 경로 패턴)으로 바꾸는 일만 하고, PR이 규칙을 어겼는지는 결정적 검사기가 판정한다.
- **규칙은 base 브랜치에서 읽는다.** PR이 README나 구조를 바꿔 자기 위반을 통과시키지 못하게 한다.
- README와 실제 구조가 다르면 README를 따르고, 차이를 코멘트에 적는다.
- 읽어낸 규칙 초안을 `.pipeline.yaml` 형식으로 PR 코멘트에 제안해, 팀이 확정하면 1순위 규칙이 되게 한다.

**공통 인터페이스**

```python
@dataclass
class Finding:
    file: str
    line: int
    severity: Literal["error", "warning"]
    message: str
    skill: str
    source: str        # 규칙의 출처. 예: "pyproject.toml", "README 3절", "base import 그래프"
    covered: bool      # 위반 줄이 테스트로 실행되는지 (F8). 자동 수정 대상 여부

def run_skill(ctx: JobContext) -> list[Finding]: ...

SKILLS = {"lint": run_lint, "static": run_static, "structure": run_structure}
```

플러그인 로더는 만들지 않는다. 스킬이 늘어 실제로 불편해질 때 구조를 키운다.

후순위: 불필요한 추상화를 AI가 지적하는 "오버엔지니어링" 스킬. 결정적 검출 수단이 없어 이번 범위에 넣지 않는다.

### F7. 수정 루프

수정안은 도구(컨벤션) 또는 AI(정적변수·구조)가 만들고, 결과의 검증은 결정적 도구가 한다.

| 스킬 | 수정 | 수정 후 검증 |
| --- | --- | --- |
| `lint` | `ruff check --fix`(안전한 수정만), 조건부 `ruff format`. AI 불필요 | 테스트 통과, 위반 0 |
| `static` | AI: 상수·설정으로 추출, 둘 위치 결정 | 테스트 통과, 원래 위반 해소, 새 위반 없음 |
| `structure` | AI: 코드 이동, 분리 | 위와 같음 |

- **시작 조건:** 위반이 있고 안전망 조건(F8)을 충족하면 PR 코멘트로 "`/fix lint`", "`/fix static`", "`/fix structure`"를 제안하고, LangGraph `interrupt`로 그래프 상태를 저장한 뒤 멈춘다. 워커는 그동안 다른 작업을 처리한다. 리뷰어가 `/fix <skill>`을 달면 `command` 작업이 등록되고 같은 그래프가 그 지점부터 재개된다. `require_approval: false`면 승인 없이 바로 수정한다.
- 체크포인트 thread_id는 `repo#pr#head_sha`다. 새 커밋이 오면 SHA가 달라져, 멈춰 있던 그래프는 별도 처리 없이 무효가 된다. 재개할 때 worktree는 다시 만든다 (F4).
- **AI 수정 루프:** ① 입력: 위반 목록과 출처, 해당 소스, 리포의 기존 상수·설정 위치 예시, (5단계부터) Chroma에서 찾은 비슷한 과거 수정 → ② AI(claude CLI)가 worktree의 파일을 고침 → ③ 결정적 검증 → ④ 실패하면 오류와 남은 위반을 AI에 다시 넘김, 최대 3회. 이 루프(작성 → 검증 → 재시도 → 제안 PR)는 하나의 서브그래프로 만들고, `static`과 `structure`가 프롬프트와 검증 함수만 바꿔 쓴다.
- `lint` 수정은 `ruff check --fix`의 안전한 수정만 적용한다(`--unsafe-fixes` 금지). `ruff format`은 base 브랜치에서 `ruff format --check`가 통과하는 리포에서만, PR이 바꾼 파일에 적용한다. base가 포맷돼 있지 않으면 PR이 건드리지 않은 줄까지 diff가 커지므로 format을 뺀다.
- 종료 조건: 테스트 통과, 원래 위반 해소, 새 위반 없음.
- **허용 변경 범위:** 수정안은 위반이 검출된 파일과 그 이동 대상만 바꿀 수 있다. 테스트 파일을 바꾸면 결과를 버린다. 예외는 코드 이동에 따른 import 경로 변경뿐이며, assert와 기대값은 바꿀 수 없다.
- **범위 검사 방법:** 수정 후 호스트에서 `git status --porcelain=v1 -z --untracked-files=all`로 worktree의 모든 변경을 본다. `git diff`는 추적 중인 파일의 수정만 보여주므로 새 파일, 삭제, 이름 변경을 놓친다. 수정(`M`), 새 파일(`??`, `A`), 삭제(`D`), 이름 변경(`R`, 옛 경로와 새 경로 둘 다)이 모두 허용 목록 안에 있어야 한다. 허용 목록 밖의 변경이 하나라도 있으면 수정안 전체를 버린다.
- 파이프라인 산출물(`.pipeline-out/`, `.venv` 마운트 지점, 확장의 `mutants/`)만 검사에서 제외한다.
- **무시된 파일:** 대상 리포의 `.gitignore`에 걸리는 파일은 `--porcelain` 출력에 나오지 않지만, 푸시되지 않아도 검증 실행에 영향을 줄 수 있다(무시된 경로에 둔 `conftest.py`, `sitecustomize.py` 등). AI를 실행하기 직전에 `git status --porcelain=v1 -z --ignored=matching --untracked-files=all`로 무시된 파일 목록을 스냅샷으로 남기고, 실행 후 같은 명령의 결과와 비교해 **새로 생긴 무시된 파일**이 있으면 수정안을 버린다. 이미 있던 무시된 파일(빌드 산출물, 로컬 캐시)은 문제 삼지 않는다.
- `structure`의 자동 수정은 1–2순위 규칙과 3순위 중 import 방향 위반에만 적용한다. 이름 패턴 위반은 알리기만 한다. 추정한 규칙으로 코드를 옮기면 틀렸을 때 비용이 크다.

### F8. 안전망 (테스트 실행과 변경 라인 커버리지)

테스트는 판정 대상이 아니라 수정안을 믿을 수 있는지 확인하는 수단이다. 수정 전 테스트 실행 결과(`PytestRun.status`)에 따라 제안할 수 있는 수정이 정해진다.

| 테스트 상태 | 뜻 | `lint` 제안 | `static`·`structure` 제안 |
| --- | --- | --- | --- |
| `passed` | 모두 통과 | O | O (테스트가 지나가는 위반 줄만) |
| `passed` + flaky | 재실행에서 통과한 테스트가 있음 | O | X (수정 후 결과를 믿을 수 없음) |
| `failed` | 실패한 테스트 또는 수집 오류가 있음 | X | X |
| `no_tests` | 실행된 테스트가 없음 | O (동작을 바꾸지 않는 변환) | X (안전망 없음) |
| `timeout` | 시간 제한을 넘김 | X | X |
| `unrunnable` | PR 코드 문제로 pytest가 제대로 실행되지 않음 (conftest·설정 오류, pytest 내부 오류, 메모리 한도 초과로 종료, 결과 파일 없음) | X | X |
| `cancelled` | 새 커밋이 와서 중단 | 새 커밋의 작업이 처리 | |
| 환경 오류 (`SandboxError`) | Docker·설치·컨테이너 문제. PR 코드 탓이 아님 | 결과를 내지 않고 작업을 큐에 되돌림 (F9) | |

- **실행 불가와 환경 오류를 나눈다.** 컨테이너 안에서 pytest가 돌았지만 PR의 conftest나 설정 때문에 결과를 못 낸 것은 `unrunnable`(PR 코드 문제)이다. docker exec 자체가 실패했거나(종료 코드 125·126·127), 결과 파일이 없는데 컨테이너가 이미 죽어 있으면 `SandboxError`(환경 오류)다.
- **의존성 설치 실패**는 pip 출력으로 원인을 나눈다 (`sandbox/venv.py`의 `classify_pip_failure`, 규칙은 `models/install.py`).

  | 원인 | pip 출력의 표지 | 처리 |
  | --- | --- | --- |
  | 해석 실패 (PR의 의존성 파일 문제) | `No matching distribution found`, `ResolutionImpossible`, `Could not find a version that satisfies`, `conflicting dependencies`, `Invalid requirement` | 1회에 `unrunnable` ("의존성을 설치하지 못함") |
  | 네트워크 실패 | `NewConnectionError`, `Temporary failure in name resolution`, `ConnectTimeoutError`, `ReadTimeoutError`, `ProxyError`, `SSLError` 등 | 환경 오류 |
  | 판단 불가 (빌드 실패 등) | 위 둘 다 없음 | 같은 의존성 해시로 2회 연속 실패하면 `unrunnable`, 그 전에는 환경 오류 |

  네트워크가 끊기면 pip이 `Could not find a version`도 함께 출력하므로 네트워크 표지를 먼저 본다. 설치 컨테이너 자체가 실패하면(종료 코드 125·126·127) pip 출력과 무관하게 환경 오류다. 연속 실패 횟수는 워커가 의존성 캐시 키별로 Redis에 센다.
- **종료 코드 137 (SIGKILL):** 먼저 취소와 시간 초과인지 본다. 그때는 파이프라인이 컨테이너를 지워서 137이 나올 수 있다. 둘 다 아닐 때만 메모리 한도 초과로 분류한다. 메모리 한도로 컨테이너 자체가 죽은 경우(docker inspect의 `OOMKilled`)도 환경 오류가 아니라 메모리 초과(`unrunnable`)다.
- **메모리 초과 안내:** 코멘트에 "실행 불가 (메모리 한도 1GB를 넘어 종료됨)"과 "메모리 한도는 1GB입니다 (현재 고정)"을 넣는다. 2단계에서 설정을 연결하면 "base 브랜치의 `.pipeline.yaml`에서 `sandbox.memory_gb`로 늘릴 수 있다"는 안내로 바꾼다. 늘릴 수 있는 상한은 파이프라인 쪽 설정(`sandbox.max_memory_gb`)이 정한다. 설정은 base 브랜치에서 읽으므로 PR이 자기 한도를 올릴 수 없다.
- **위반 줄의 커버리지:** 샌드박스가 남긴 `coverage.xml`로 위반 줄이 테스트로 실행되는지 본다. 실행되지 않는 줄의 위반은 "테스트가 지나가지 않아 자동 수정 대상 아님"으로 표시하고 알리기만 한다. 고친 뒤 테스트가 통과해도 그 줄의 동작을 확인한 것이 아니기 때문이다.
- PR 전체의 변경 라인 커버리지(`diff-cover`)는 참고 지표로 코멘트에 적는다. diff-cover는 PR 코드를 실행하지 않으므로 호스트에서 돌린다. 입력은 샌드박스가 남긴 `coverage.xml`과 호스트에서 만든 `changes.diff`다.
- `coverage.xml`의 경로는 컨테이너 기준(`/work/...`)이 아니라 리포 기준 상대 경로여야 한다. coverage의 `relative_files = true`를 파이프라인 쪽 설정 파일에 두고 `--cov-config`로 넘긴다. 1단계 구현에서는 대상 리포의 coverage 설정을 쓰지 않고 파이프라인 설정(`docker/sandbox/coveragerc`: `relative_files`, `source = .`, `.venv/` 제외)만 쓴다. 기본 `# pragma: no cover`는 그대로 동작한다. 리포 설정의 `omit`·`exclude_lines`를 반영할지는 협업 리포 검증 때 다시 본다.
- 실패한 테스트는 한 번 재실행한다. 재실행에서 통과하면 flaky로 표시한다.

### F9. 결과와 PR 코멘트

코멘트 맨 위는 두 줄이다. 테스트는 글자로만 쓰고, 기호(⚠️/✅)는 협업 규칙에만 쓴다. ⚠️는 "위반 있음"이라는 뜻으로만 쓴다.

```
**테스트:** 통과 (9개)
**규칙:** ⚠️ 위반 3건 (lint 2, static 1) · 자동 수정 가능: /fix lint
```

| 줄 | 표시 |
| --- | --- |
| 테스트 | `통과 (N개)`, `통과, 불안정한 테스트 N개`, `실패 (N개 중 M개)`, `실행된 테스트 없음`, `시간 초과 (N초)`, `실행 불가 (이유)` |
| 규칙 | `⚠️ 위반 N건 (스킬별 건수)` 또는 `✅ 위반 없음`. 2단계부터 표시한다 |

- 환경 오류일 때는 테스트·규칙 결과를 쓰지 않는다. 진행 표시만 "환경 문제로 대기 중"으로 바꾸고 작업을 큐에 되돌린다. 실패처럼 보이는 표시를 PR에 남기지 않는다.
- 테스트가 실패해도 규칙 줄은 그대로 보여준다. 위반 정보는 유효하고, 수정 제안만 막힌다 (F8).
- PR당 코멘트는 하나만 달고, 진행하면서 같은 코멘트를 수정한다 (진행률, 단계별 결과, 소요 시간).
- 두 줄 아래에는 스킬별 위반 목록(파일·줄·내용), 적용한 규칙의 출처, 자동 수정 대상이 아닌 위반과 그 이유, 테스트 상세(실패한 테스트, 실행 불가 출력), 이번 검사의 AI 비용을 넣는다.
- **출처 표시:** "적용한 규칙: ruff(pyproject.toml), 의존 방향(README 3절), 이름 패턴(base 파일 구조)"처럼 남긴다.

### F10. 제안 PR

| 종류 | 허용 변경 | 브랜치 |
| --- | --- | --- |
| 수정 제안 | 위반이 검출된 파일과 그 이동 대상 (F7의 제한) | `pipeline/pr-<n>-fix-<skill>` |
| 테스트 제안 (확장, F13) | `tests/` 안만 | `pipeline/pr-<n>-tests` |

- 푸시 조건: F7의 종료 조건, 허용 범위 안의 변경, 개수·크기 상한 이내, 비밀값 검사 통과(보안 절 참고).
- `[pipeline]` 커밋으로 올리고, 원본 PR 브랜치를 base로 PR을 연다 (`gh pr create`).
- **머지 여부는 커밋 메시지가 아니라 GitHub에서 확인한다.** merge commit은 리뷰어 이름의 `Merge pull request ...` 커밋이 되고, squash·rebase 머지는 SHA가 새로 만들어져 메시지나 조상 관계로는 알 수 없다. `proposals`에 저장한 제안 PR 번호로 머지 여부와 머지 커밋 SHA를 조회해(`gh pr view --json state,mergeCommit`) 원본 PR의 head와 비교한다.
- 커밋할 때는 허용된 경로만 지정해서 스테이징한다. `git add -A`를 쓰지 않는다. `.pipeline-out/` 같은 산출물이 제안 PR에 섞이지 않게 한다.
- 본문: 해소한 위반과 규칙의 출처, 바꾼 이유, 테스트 결과.
- 원본 PR에 "lint 위반 N건 수정 제안 → #번호" 같은 코멘트를 남긴다.

| 상황 | 처리 |
| --- | --- |
| 원본 PR에 새 커밋 | 제안이 여전히 유효하면 유지, 깨지면 닫고 다시 생성 |
| 제안 PR 머지 → 원본에 새 커밋 | head가 제안 PR의 머지 커밋이거나 그 뒤에 새 커밋이 없으면 검사만 다시 하고, 같은 종류의 제안은 다시 만들지 않음 |
| 원본 PR 머지·종료 | 남은 제안 PR을 닫고 브랜치 삭제 |
| 작성자가 제안 PR을 닫음 | 같은 커밋에 대해 다시 제안하지 않음. 거절 기록으로 남김 |

### F11. 수동 명령 (PR 코멘트)

- `/recheck`: 현재 커밋으로 다시 검사
- `/fix <lint|static|structure>`: 멈춰 있는 수정 단계를 승인하고 재개
- 확장(F13): `/mutate`, `/generate-tests`

명령은 F1의 작성자 권한 조건을 통과한 것만 처리하고, 처리한 코멘트 ID를 저장한다.

확장 명령이 이 리포에서 꺼져 있으면(`.pipeline.yaml`의 `extensions.<이름>.enabled: false`, 기본값) 실행하지 않고 "`/mutate`는 이 리포에서 꺼져 있습니다 (`extensions.mutation.enabled`)" 같은 코멘트를 남긴다. 조용히 무시하면 명령을 단 사람은 처리 중인지 알 수 없다. 이 경우에도 코멘트 ID를 저장해 같은 안내를 두 번 달지 않는다.

### F12. 운영 알림 (Slack)

협업 결과는 PR 안에 두고, Slack은 PR에 표시되지 않는 운영 문제만 받는다.

| 종류 | 예시 | 받는 곳 |
| --- | --- | --- |
| 파이프라인 오류 | gh·claude 로그인 만료, Docker 중단, 워커 다운, 작업 반복 실패 | 운영자 개인 채널 |
| 확인 요청 (선택) | "PR #14 ⚠️ 위반 3건, 수정 제안 있음" 한 줄 + PR 링크 | 팀 채널 |

- Slack Incoming Webhook URL 하나로 보낸다. URL은 비밀값 파일로 둔다.
- 같은 오류는 해결될 때까지 30분에 한 번만 보낸다.
- PR 결과 본문을 Slack에 복사하지 않는다. 내용은 PR에서 본다.

### F13. 선택 확장: 테스트 품질 (7단계)

메인 기능이 끝난 뒤 필요하면 붙인다. 붙이지 않아도 F1–F12는 그대로 동작한다.

- **수상한 테스트 탐지 (정적 검사):** assert가 없거나 `assert True`인 테스트, 테스트 대상 자체를 mock한 테스트, 새로 skip 처리된 테스트, 같은 PR에서 assert를 지운 변경. 기대값을 바꾼 변경은 기능 변경과 구분할 수 없으므로 "리뷰어 확인 필요"로만 표시한다.
- **바뀐 줄 뮤테이션:** 바뀐 줄에서 나온 뮤턴트만 `mutmut`으로 실행한다. 트리거는 `non_draft`(기본) / `every_push` / `manual`(`/mutate`만), PR당 뮤턴트 상한(기본 50개)과 뮤턴트별 타임아웃을 둔다. 결과는 뮤테이션 점수와 살아남은 뮤턴트 목록이다. 넣으면 F8의 안전망 조건에 "위반 줄의 뮤턴트가 잡힘"을 더할 수 있다.
- **AI 테스트 생성:** 커버되지 않은 줄과 살아남은 뮤턴트를 입력으로 AI가 `tests/` 아래에 테스트를 쓰고, pytest 통과 + 목표 뮤턴트 처치로 검증해 테스트 제안 PR로 올린다(`/generate-tests` 승인). F7의 AI 수정 서브그래프를 프롬프트와 검증 함수만 바꿔 재사용한다. 채택되면 안전망이 넓어져, 전에는 자동 수정 대상이 아니던 위반을 고칠 수 있게 된다.
- **0단계 스파이크 결과 (mutmut 3.8.1, 예제 리포 `orders`):**
  - 뮤턴트 지정 실행은 된다. 이름은 `모듈.x_함수__mutmut_N` 형식이고 glob(`모듈.x_함수*`)도 받는다. 지정하지 않은 뮤턴트는 `not checked`로 남는다.
  - "바뀐 줄만"은 기본 기능이 없다. 다음 순서로 구현한다: ① 매칭되지 않는 이름으로 `mutmut run`을 돌려 뮤턴트만 생성(실행 0개, 종료 코드 1) → ② `mutmut show <이름>`의 diff(함수 기준 줄 번호)와 AST의 함수 시작 줄로 뮤턴트의 실제 줄을 계산 → ③ `changes.diff`의 바뀐 줄에 해당하는 뮤턴트만 골라 상한(50개)까지 이름을 지정해 실행.
  - 결과는 `mutmut results --all true`와 `mutants/` 아래 `.meta` JSON(뮤턴트별 종료 코드), `mutmut export-cicd-stats`로 읽는다.
  - 한계: mutmut 3은 함수 안의 코드만 변형한다. 모듈 수준 상수의 변경은 뮤턴트가 나오지 않는다.
  - mutmut은 worktree 안에 `mutants/` 폴더를 만든다. git이 필요 없어 컨테이너 안에서 돌릴 수 있다. `mutants/`도 "허용 범위 밖 변경" 검사와 스테이징에서 제외한다.

## 설정

별도 초기화 명령 없이, 워커가 PR을 체크아웃할 때 리포 안의 정보를 읽어 규칙을 정한다. 설정과 규칙은 PR의 head가 아니라 **base 브랜치**에서 읽는다. PR이 기준값을 낮춰 자기 검사를 통과시키지 못하게 하기 위해서다.

**참조 우선순위**

1. `.pipeline.yaml` (있으면 이것이 최우선)
2. 실제 도구 설정 파일: `pyproject.toml`의 `[tool.ruff]`, `[tool.pytest.ini_options]` 등
3. README·CONTRIBUTING에 적힌 규칙: AI가 읽고 규칙 형식으로 바꾼다. ⚠️ 경고로만 쓴다.
4. base 브랜치의 실제 파일 구조 (F6)
5. 기본 규칙

설정 파일과 README가 다르면 설정 파일을 따른다.

**일관성 보장:** README에서 뽑은 규칙은 README의 git blob 해시와 묶어 DB에 저장한다. README가 바뀌지 않으면 저장된 규칙을 그대로 쓴다. 파일 구조에서 읽은 규칙은 base 커밋의 tree 해시와 묶는다. `.pipeline.yaml`이 없는 리포에는 추론한 설정 초안을 PR 코멘트로 제안한다.

**`.pipeline.yaml` 스키마 (초안, 대상 리포에 둠)**

```yaml
tests:
  runner: pytest

sandbox:
  memory_gb: 1                 # 테스트 컨테이너 메모리 한도. 파이프라인 쪽 max_memory_gb를 넘을 수 없음

skills:
  enabled: [lint, static, structure]
  fix:
    require_approval: true     # /fix 승인 후 수정안 생성 (interrupt)
    require_covered: true      # static·structure는 테스트가 지나가는 위반 줄만 자동 수정 (F8)
    max_retries: 3
  static:
    min_repeats: 3             # 같은 리터럴이 이 횟수 이상 반복되면 위반
  structure:
    source: auto               # auto(README·파일 구조에서 읽기) | explicit
    forbidden_imports: []      # explicit일 때. 예: [[app.controllers, app.repositories]]
    path_patterns: {}          # 예: {app/services: "*_service.py"}

model:
  backend: claude-cli          # claude-cli | codex-cli | api | local
  light: haiku
  heavy: sonnet
  budget_per_pr_usd: 0.50

notify:
  slack:
    ops: true
    team: [violation, fail]

extensions:                    # 선택 확장 (F13, 7단계). 기본은 모두 끔
  mutation:
    enabled: false
    trigger: non_draft         # non_draft | every_push | manual
    max_mutants: 50
  ai_tests:
    enabled: false
    require_approval: true
```

**파이프라인 쪽 설정 (`~/.config/pipeline/config.yaml`, 대상 리포 밖)**

```yaml
repos: [owner/name]            # 처리할 리포 허용 목록
sync:
  interval_minutes: 5
sandbox:
  max_memory_gb: 4             # 대상 리포가 sandbox.memory_gb로 올릴 수 있는 상한
```

수치는 초깃값이며, 평가 결과(측정 지표 섹션)를 보고 조정한다.

## 보안·인증

워커는 로컬에서 로그인된 `gh`와 `claude`를 쓴다. PR에서 온 코드와 AI가 쓴 코드는 샌드박스에서만 실행한다.

### Preflight 점검

워커는 시작할 때 아래를 확인하고, 하나라도 실패하면 시작하지 않는다.

| 항목 | 확인 방법 | 실패 시 안내 |
| --- | --- | --- |
| GitHub 로그인 | `gh auth status` | `gh auth login` |
| Claude 로그인 | 짧은 요청 1회 (전용 상태 명령이 있으면 그것 사용) | `claude` 실행 후 로그인 |
| Docker | `docker info` | Docker 실행 |
| Redis | `redis-cli ping` 또는 클라이언트 ping | `docker compose up -d` |
| Chroma (5단계부터) | heartbeat 요청 | `docker compose up -d` |

실행 중 인증이 풀리거나 Docker가 멈추면 환경 오류로 처리한다. PR에 결과를 쓰지 않고, 작업을 큐에 되돌리고, 워커는 새 작업 받기를 멈추고, Slack 운영 알림을 보낸다.

### 비밀값

- 코드·Dockerfile `ENV`·환경변수에 넣지 않는다.
- 필요한 비밀값(웹훅 시크릿, Slack URL)은 `~/.config/pipeline/` 아래 파일(`chmod 600`)에서 읽어 메모리에만 둔다.
- GitHub과 Claude는 로컬 로그인을 재사용해 별도 토큰을 보관하지 않는다.
- subprocess에는 최소 환경(`PATH` 등)만 넘긴다.
- 팀 서버로 옮길 때는 GitHub App 개인키로 작업마다 1시간짜리 토큰을 발급하는 방식으로 바꾼다.

### 샌드박스 (1단계부터)

**도입 이유:** PR의 테스트 코드와 `conftest.py`는 pytest가 그대로 실행한다. 호스트에서 돌리면 PR을 올릴 수 있는 사람이 gh·claude 로그인이 있는 계정에서 코드를 실행하게 된다. 이후 단계에서는 AI가 고친 코드(무한 루프, 메모리 폭주 가능)도 사람 확인 없이 실행된다.

```bash
docker run -d --name job-<id> --network none \
  --memory 1g --cpus 1 --pids-limit 256 --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v /tmp/pipeline/pr-<n>:/work -w /work \
  -v ~/.cache/pipeline/venvs/<cache-key>:/work/.venv:ro \
  -v <pr-proof>/docker/sandbox:/pipeline:ro \
  pipeline-sandbox sleep infinity
docker exec -e PYTHONPATH=/work job-<id> timeout 600 .venv/bin/pytest \
  -p no:cacheprovider \
  --json-report --json-report-file=.pipeline-out/report.json \
  --cov --cov-config=/pipeline/coveragerc \
  --cov-report=xml:.pipeline-out/coverage.xml
docker rm -f job-<id>
```

- 작업당 컨테이너 하나, 명령은 `docker exec`로 넣는다.
- 컨테이너는 호스트 사용자의 UID·GID로 실행한다. 고정 UID(1000)는 Linux에서 호스트 UID가 다르면 `.pipeline-out/`에 쓰지 못한다. 컨테이너에 없는 UID라 홈 디렉터리가 없으므로 `HOME=/tmp`를 준다.
- 이미지는 Python과 검사 도구만 든 베이스 이미지 하나다. 대상 리포의 의존성은 이미지가 아니라 `.venv`에 설치하고, 캐시 키별로 보관해 테스트 컨테이너에는 읽기 전용으로 마운트한다 (F5). 설치는 네트워크를 쓰지만 컨테이너 안에서 일어나고 비밀값이 없다.
- 비밀값은 샌드박스에 절대 넣지 않는다.
- 워커는 호스트의 docker CLI로 컨테이너를 다룬다. 워커가 로컬 프로세스라 docker.sock을 컨테이너에 마운트할 일이 없다.

### AI(claude CLI) 권한

```bash
claude -p "$PROMPT" \
  --restricted --strict-mcp-config \
  --permission-mode dontAsk \
  --allowedTools "Read,Grep,Glob,Write,Edit" \
  --model haiku --max-budget-usd 0.30 --output-format json
# cwd = PR worktree, Bash·웹 도구 없음
# 0단계 스파이크에서 확인한 옵션 (claude CLI 2.1.326)
```

- 작업 폴더는 PR worktree로 고정한다. 홈 디렉터리에서 실행하지 않는다.
- **읽기·쓰기를 worktree 안으로 제한한다.** 제한이 없으면 다음 경로가 열린다: PR에 심은 지시문 → `~/.config/pipeline/`이나 `~/.ssh`를 읽음 → 그 내용을 수정 파일에 씀 → 제안 PR로 GitHub에 올라감.
- **PR에 들어 있는 `.claude/` 설정(hooks 포함)이 로드되지 않게 한다.**
- **0단계 스파이크 결과 (claude CLI 2.1.326):** `--restricted`가 위 두 가지를 맡는다. Bash 같은 실행 도구와 WebFetch를 빼고, 파일 도구를 작업 폴더 안으로 제한하고, user·project·local 설정 파일을 읽지 않는다. 실제로 확인한 것: worktree 밖 파일의 Read·Write가 거부됨, worktree 안 쓰기는 됨, worktree의 `.claude/settings.json`에 넣은 hook이 실행되지 않음, Bash 없음.
- JSON 출력에 `total_cost_usd`, `num_turns`, `permission_denials`(거부된 도구 호출 목록)가 들어 있다. 비용 기록과 "허용 범위 밖 시도" 탐지에 쓴다.
- 로그인 확인은 `claude auth status`(JSON, `loggedIn`과 `loginExpiresAt`)로 한다. preflight가 만료 시각도 본다.
- `--max-turns`는 도움말에 없어 쓰지 않는다. 상한은 `--max-budget-usd`로 건다.
- 확인하지 못한 것: `--restricted`에서 worktree의 `CLAUDE.md`가 프롬프트에 들어가는지. 들어가더라도 경로 제한과 실행 후 diff 검사가 막지만, 3단계에서 다시 확인한다.
- 실행 후 `git status --porcelain --untracked-files=all`로 허용 범위(F7) 밖의 변경을 확인해 있으면 버린다. 새 파일, 삭제, 이름 변경도 확인한다. 무시된 파일은 AI 실행 직전 스냅샷과 비교해 새로 생긴 것만 잡는다.
- **푸시 전 비밀값 검사:** 제안 PR의 diff에 파이프라인이 가진 비밀값(웹훅 시크릿, Slack URL)이나 토큰·개인키 패턴이 있으면 버리고 운영 알림을 보낸다.

## 비용 최적화·모델 비교·회귀 테스트

기본은 가벼운 모델이고, 가벼운 모델이 실패한 경우에만 고성능 모델로 올린다. 두 모델의 차이는 평가 세트로 측정해 숫자로 정한다.

### 비용 최적화

1. **AI를 안 부르는 것이 기본:** 테스트 실행·커버리지·린트·정적변수·구조 검사는 결정적 도구라 비용이 없다. 위반이 없으면 AI를 호출하지 않는다. 컨벤션 수정도 AI 없이 한다.
2. **리뷰어 승인 후 수정:** `/fix` 승인이 있을 때만 AI를 부른다 (설정으로 끌 수 있음).
3. **범위 축소:** 바뀐 줄의 위반만, 테스트가 지나가는 위반만 자동 수정.
4. **재사용:** README 규칙은 blob 해시로, 파일 구조 규칙은 base tree 해시로 캐시해 바뀔 때만 다시 만든다.
5. **상한:** PR당 예산(`budget_per_pr_usd`), 호출당 `--max-budget-usd`, 재시도 3회. 예산을 넘으면 수정을 멈추고 코멘트로 알린다.
6. **비용 표시:** PR 코멘트에 AI 호출 횟수와 추정 비용을 남기고 DB에 기록한다.
7. **백엔드 선택:** 구독 계정(claude CLI 로그인, 개인·로컬 사용), API 키(팀 서버), 로컬 모델(Ollama, 비용 0). 구독 계정을 여러 사람의 자동화에 쓸 때는 약관을 확인한다.

### 가벼운 모델 vs 고성능 모델

| 항목 | 가벼운 모델 (예: Haiku급, 로컬 소형 모델) | 고성능 모델 (예: Sonnet·Opus급) |
| --- | --- | --- |
| 기본 역할 | 모든 첫 시도 | 가벼운 모델 실패 시에만 |
| 맡길 일 | 한 파일 안의 상수 추출, README 규칙 변환 | 여러 파일에 걸친 상수·설정 정리, 코드 이동·분리 |
| 속도·비용 | 빠르고 싸다 | 느리고 비싸다 |
| 예상 약점 | 이동 뒤 import 누락, 새 위반 생성, 재시도 증가 | 비용, 불필요하게 큰 변경 |

**라우팅 규칙 (초안)**

- 가벼운 모델이 2회 연속 실패(테스트 실패, 위반 미해소, 새 위반) → 고성능 모델로 1회
- 대상 코드가 3개 이상 모듈을 import하거나 여러 파일을 옮기는 수정 → 처음부터 고성능 모델
- 예산의 80%를 쓰면 고성능 모델로 올리지 않는다

**비교 실험:** 평가 세트를 네 가지 조건으로 돌린다: 가벼운 모델만, 고성능 모델만, 라우팅, 가벼운 모델 + 과거 기록 검색. 조건마다 위반 해소율, 수정 후 테스트 통과율, 새 위반 발생률, 평균 재시도, 토큰, 소요 시간, **채택 가능한 수정 1개당 비용**을 기록한다. 라우팅 규칙의 기준값은 이 결과로 정한다.

### 회귀 테스트

**대상 리포의 회귀 방지**

| 시점 | 실행 범위 |
| --- | --- |
| 새 커밋 | 전체 테스트 (수정 전 기준선) |
| 수정안 검증 | 전체 테스트 (수정이 기존 동작을 깨지 않는지) |
| 제안 PR 머지 후 | 새 커밋으로 다시 검사 |

**파이프라인 자체의 평가 세트**

- 작은 예제 리포에 위반을 일부러 넣은 케이스(컨벤션 위반, 하드코딩 값·전역 상태, 기존 구조에 없던 import 방향)를 만든다. 넣은 것을 알기 때문에 정답이 있다. 테스트가 지나가지 않는 위반, 수정하면 테스트가 깨지는 위반처럼 안전망이 걸러야 하는 케이스도 넣는다.
- 예제 리포는 파이프라인 리포와 분리한 별도 GitHub 리포에 둔다. 첫 예제는 `Eunryong/eval`(주문 금액 계산 `orders`)이고, 파이프라인의 첫 대상 리포로 등록해 실제 PR과 웹훅으로 각 단계를 확인한다. 파이프라인 자체의 통합 테스트는 이 리포를 클론해 쓴다. 파이프라인 리포(`pr-proof`) 자체는 대상으로 등록하지 않는다.
- 구조 검증이 특정 패턴에 묶이지 않음을 보이기 위해 구조가 다른 예제를 둘 이상 둔다(예: 레이어별 폴더, 기능별 폴더). 예제를 늘릴 때는 리포를 추가한다.
- 스킬을 넣는 단계마다 그 스킬의 케이스 3–5개를 E2E 테스트로 추가하고, 6단계에서 20–30개로 늘린다.
- 프롬프트, 모델, 라우팅 규칙을 바꿀 때마다 평가 세트를 돌리고, DB에 저장된 기준선과 비교한다. 해소율이나 비용이 기준선보다 나빠지면 변경을 채택하지 않는다.

**실제 협업 리포에서의 검증**

- 실제 PR을 대상으로 한 검증은 운영자가 협업 리포에서 직접 사용하면서 나중에 한다. 대상 리포와 시점은 그때 정한다.
- 그 전까지 각 단계의 완료 기준은 평가 세트와 저장해 둔 웹훅 페이로드로 돌리는 자동 테스트다. GitHub 호출은 `github.py` 뒤에 두어 테스트에서 대체한다.
- 위반 비율, 채택률 같은 실사용 지표는 협업 리포 검증에서 나온다.

## 벡터DB (Chroma)

코드를 임베딩하는 대신 **팀의 과거 기록**을 쌓아, 비슷한 상황이 오면 AI에 참고로 넣는다.

| 기록 | 검색 키 (임베딩) | 꺼내 쓰는 것 |
| --- | --- | --- |
| 위반 → 채택된 수정 | 위반 규칙, 해당 코드 요약 | 머지된 수정 diff (상수를 어디에 두는지, 코드를 어디로 옮기는지) |
| 거절된 제안 | 위와 같음 | "이렇게는 하지 말 것" 예시 |
| 검증에 실패한 수정 | 위반 규칙, 실패 내용 | 테스트를 깨뜨린 수정 패턴 |

- **저장소:** Chroma를 Docker 컨테이너로 띄운다 (`docker compose`, Redis와 함께). 기록의 원본(커밋 해시, diff, PR 번호)은 SQLite에 두고, Chroma에는 임베딩과 기록 ID만 넣는다. Chroma 볼륨이 지워져도 SQLite에서 다시 만들 수 있다.
- 커밋 내역 전체는 저장하지 않는다. git에 있으니 해시만 참조하고, diff는 관련 부분만 최대 N줄 저장한다.
- **자동 수집:** 제안 PR은 머지되면 채택 기록, 닫히면 거절 기록이 된다. 작성자가 제안 PR 대신 자기 커밋으로 위반을 고치면(다음 검사에서 위반이 사라짐) 그 diff도 채택 기록으로 저장한다. 수정 루프에서 검증에 실패한 시도는 실패 기록이 된다.
- **검색 분리:** 같은 규칙·같은 파일처럼 정확히 찾을 것은 SQL로, 비슷한 에러·비슷한 위반은 Chroma로 찾는다. 코드 맥락(누가 이 함수를 호출하는지)은 정적 분석으로 찾는다.
- **임베딩 모델:** 5단계에서 정한다. 외부 API 없이 로컬에서 계산하는 것을 기본으로 한다.
- **데이터 부족 대비:** 로컬에서 쓰면 기록이 몇 건 쌓이지 않는다. 평가 세트를 돌리면서 나온 기록으로 채운다.
- **효과 측정:** 평가 세트에서 가벼운 모델 단독과 가벼운 모델 + 검색의 위반 해소율을 비교한다. 효과가 없으면 그 결과를 그대로 기록하고 기본값을 끔으로 둔다.

## 프로젝트 구조와 기술 스택

한 리포, 한 Python 프로젝트에서 api와 worker를 진입점 두 개로 나눠 실행한다.

**기술 스택**

| 영역 | 선택 | 이유 |
| --- | --- | --- |
| 웹훅 서버 | FastAPI + uvicorn | 비동기, 빠른 응답(202), 타입 기반 요청 검증 |
| 큐 | Redis + RQ | 웹훅 10초 제한, 작업 보존·재시도, 동시 실행 수 제어. Celery는 이 규모에 과함 |
| 작업 흐름 | LangGraph | 단계별 상태, 조건 분기(모델 라우팅), interrupt(리뷰어 승인 대기), checkpointer(재시작 후 이어가기), 생성·수정 루프의 서브그래프 재사용 |
| 영구 저장 | SQLite + SQLModel | 로컬 도구라 서버 불필요. 이력·캐시·지표·기록 원본·LangGraph 체크포인트. 팀 서버에서는 PostgreSQL로 교체 |
| 벡터 검색 | Chroma (Docker) | 과거 기록의 유사 검색 |
| 검출·검증 | ruff, (semgrep), AST 검사, pytest, coverage, diff-cover, (확장: mutmut) | 결정적 판정 |
| AI | claude CLI (기본), codex CLI·API·Ollama (선택) | 수정안 작성, README 규칙 변환 |
| GitHub 연동 | gh CLI | 로컬 로그인 재사용, PR 상태 조회, 코멘트·PR 생성 |
| 컨테이너 | Docker, docker compose | 샌드박스(작업마다), Redis·Chroma(상시) |
| 웹훅 터널 | cloudflared 또는 smee.io | 로컬 api로 웹훅 수신 |
| 알림 | Slack Incoming Webhook | 운영 오류 |

**디렉터리 구조**

코드는 프로세스 역할에 따라 나눈다.

- **웹(api):** GitHub 변화를 감지해 `PRJob`을 큐에 넣는다. 감지 방법은 웹훅(GitHub이 알려줌)과 동기화(우리가 물어봄) 두 가지다. PR 코드를 실행하지 않고, 워커가 무엇을 했는지 몰라도 된다. MVC로 짠다.
- **워커:** 큐에서 작업을 꺼내 처리하고 결과(코멘트, 제안 PR)를 GitHub에 남긴다. 작업이 웹훅에서 왔는지 동기화에서 왔는지 몰라도 된다.
- **둘 사이:** 큐(`PRJob`)와 Redis·SQLite에 남긴 상태만 오간다. 웹이 Redis에 쓴 최신 head를 워커가 읽고, 워커가 SQLite에 쓴 처리 기록을 동기화가 읽는다. 키 형식과 테이블 정의는 공용 `models/`에 두고, 실제 읽기·쓰기 코드는 각자 자기 쪽에 둔다.
- **GitHub 호출도 각자 갖는다.** 웹은 조회(열린 PR, head SHA, 코멘트 목록), 워커는 쓰기(코멘트, 제안 PR)만 해서 겹치지 않는다.

| 패키지 | 역할 | import 가능 |
| --- | --- | --- |
| `models/` | 웹과 워커가 같이 쓰는 데이터: `PRJob`, 테스트 결과, Redis 키 형식, DB 테이블 정의. 외부 호출 없음 | models |
| `controllers/` | 웹: 요청 하나(웹훅 한 건, 동기화 한 번)를 받아 서비스를 부르고 뷰로 응답을 만든다 | models, services, views |
| `services/` | 웹: 서명 검증, 큐 등록과 중복 방지, 최신 head 기록, GitHub 조회, 처리 기록 조회 | models |
| `views/` | 웹: 모델을 API 응답으로 바꾼다 | models |
| `worker/` | 워커: 진입점, 작업 흐름(LangGraph), PR 코멘트, 처리 기록 쓰기, git·GitHub 쓰기 | models, sandbox |
| `sandbox/` | 워커가 쓰는 docker 도구. PR 코드는 여기를 거쳐서만 실행한다 | models |

웹 쪽(controllers·services·views)은 `worker/`와 `sandbox/`를, 워커는 웹 쪽을 import하지 않는다. 의존 방향은 `tests/test_layers.py`가 검사한다.

```
pipeline/
├── app/
│   ├── api.py                   # 진입점: FastAPI 앱 (uvicorn), lifespan에서 동기화 루프 시작
│   ├── cli.py                   # 수동 명령: python -m app.cli sync
│   ├── config.py                # 설정 로드, 비밀값 파일 읽기
│   ├── models/                  # 웹·워커 공용 데이터
│   │   ├── job.py               # PRJob: 작업 데이터 계약, 작업 키
│   │   ├── test_run.py          # PytestRun, ReportSummary, 실행 상태
│   │   ├── state_keys.py        # Redis 키 형식 (최신 head, 작업 키)
│   │   └── db.py                # SQLModel 테이블 정의
│   ├── controllers/             # 웹
│   │   ├── webhook.py           # 웹훅 감지 → PRJob → 큐
│   │   └── sync.py              # 동기화: GitHub 조회 → 처리 기록과 비교 → PRJob → 큐
│   ├── services/                # 웹
│   │   ├── signature.py         # 웹훅 서명 검증
│   │   ├── queue.py             # 큐 등록, 작업 키 중복 방지, 최신 head 기록 (Redis)
│   │   ├── github.py            # GitHub 조회: 열린 PR, head SHA, 코멘트 목록 (gh CLI)
│   │   └── history.py           # 처리 기록 조회 (SQLite)
│   ├── views/                   # 웹
│   │   └── responses.py         # API 응답
│   ├── worker/                  # 워커
│   │   ├── main.py              # 진입점: RQ 워커, preflight
│   │   ├── graph.py             # 작업 흐름: LangGraph 노드와 분기 (지금은 run_pr_tests())
│   │   ├── report.py            # PR 코멘트 마크다운 (결과, 위반과 출처, 수정 제안, 테스트, 비용)
│   │   ├── state.py             # 처리 기록·생존 확인 쓰기 (SQLite), 최신 head 확인 (Redis)
│   │   ├── repo.py              # bare clone 캐시, worktree, changes.diff (git, 호스트)
│   │   ├── github.py            # GitHub 쓰기: post_comment, update_comment, open_pr (gh CLI)
│   │   ├── skills/              # lint, static, structure: 검출과 수정
│   │   ├── safety.py            # 안전망: 위반 줄 커버리지, diff-cover
│   │   ├── extensions/          # 선택 확장: suspicious, mutation, ai_tests (7단계)
│   │   ├── rules.py             # 구조 규칙 읽기: README 변환, import 그래프, 이름 패턴
│   │   ├── ai/                  # 모델 백엔드, 프롬프트, 라우팅
│   │   ├── memory.py            # 기록 저장과 검색 (SQLite + Chroma)
│   │   └── notify.py            # Slack
│   └── sandbox/                 # 워커가 쓰는 docker 도구
│       ├── docker_cli.py        # docker 호출, 최소 환경, 자원 제한, 베이스 이미지, SandboxError
│       ├── venv.py              # 의존성 파일, 캐시 키, .venv 설치와 캐시
│       ├── container.py         # 작업 컨테이너 띄우기, run_in_sandbox() (취소·타임아웃)
│       └── pytest_runner.py     # pytest 실행, report.json·coverage.xml 읽기
├── docker/sandbox/              # 샌드박스 베이스 이미지(Dockerfile), coverage 설정
├── eval/                        # 위반 주입 케이스, 실행 스크립트 (예제 리포는 별도 GitHub 리포, 2단계부터)
├── tests/                       # 파이프라인 자체 테스트. app/과 같은 폴더로 나눈다
├── docker-compose.yml           # redis, chroma
└── Procfile                     # api, worker
```

파일은 필요한 단계에서 만든다. `skills/`는 2–4단계(lint → static → structure), `rules.py`는 4단계, `memory.py`와 compose의 chroma는 5단계, `extensions/`는 7단계에 추가한다.

**작업 데이터 계약 (웹 ↔ 워커)**

```python
class PRJob(BaseModel):
    repo: str                  # "owner/name"
    pr_number: int
    kind: Literal["update", "command", "close"]
    head_sha: str | None       # issue_comment 웹훅에는 없다. 없으면 워커가 조회
    base_sha: str | None
    command: str | None        # "/recheck", "/fix lint" ... (확장: "/mutate", "/generate-tests")
    comment_id: int | None     # 명령의 코멘트 ID
    source: Literal["webhook", "sync"]
    delivery_id: str | None    # 웹훅으로 온 작업에만 있다
```

프로세스들은 메모리를 공유하지 않는다. 이 형식이 유일한 계약이고, 상태는 Redis(임시)와 SQLite(영구)에 둔다. 같은 리포에서 같은 버전으로 실행한다.

**SQLite 테이블 (초안)**

| 테이블 | 내용 |
| --- | --- |
| `runs` | PR·커밋별 검사 이력, 상태(대기·실행 중·완료), 워커 생존 확인 시각, 판정, 점수, 소요 시간, AI 비용, 출처(webhook·sync) |
| `findings` | 스킬 위반(파일, 줄, 규칙, 출처, 커버 여부), 확장 사용 시 살아남은 뮤턴트 |
| `proposals` | 제안 PR, 스킬(lint·static·structure, 확장: tests), 상태(열림·머지·닫힘) |
| `commands` | 처리한 명령의 코멘트 ID |
| `rules` | README blob 해시·base tree 해시 → 읽어낸 규칙 |
| `records` | 위반 → 채택된 수정, 거절된 제안, 검증에 실패한 수정 (Chroma 임베딩의 원본) |
| `eval_runs` | 평가 세트 실행 결과와 기준선 |

**실행**

```
docker compose up -d      # redis (5단계부터 chroma)

# Procfile — honcho start
api:    uvicorn app.api:app --port 8000
worker: python -m app.worker.main
```

## 단계별 구현 계획

각 단계는 동작하는 상태로 끝낸다. 완료 기준은 평가 세트의 예제 리포와 저장해 둔 웹훅 페이로드로 돌리는 자동 테스트다. 실제 협업 리포에서의 검증은 나중에 따로 한다.

**검증 대상:** 첫 대상 리포는 예제 리포 `Eunryong/eval`이다. 파이프라인 작업은 기능 단위 브랜치와 PR로 진행하고, 파이프라인 자체의 단위·통합 테스트(`pytest`)로 확인한다. 파이프라인 리포는 자기 PR을 검사하지 않는다.

### 0단계. 준비

- [x] 파이프라인 리포 생성, 디렉터리 구조·Procfile·docker-compose·pyproject 작성
- [x] 스파이크: mutmut으로 바뀐 줄의 뮤턴트만 실행, 뮤턴트 하나를 지정해 재실행 (결과는 F13)
- [x] 스파이크: claude CLI의 로그인 확인 명령, 경로 단위 권한 제한, 프로젝트 설정 차단 옵션 (결과는 보안·인증)
- [x] 첫 예제 리포 `orders`를 별도 리포 `Eunryong/eval`로 분리 (README에 구조 설명이 있고 폴더 구조가 일관된 작은 Python 프로젝트)

### 1단계. 수신 → 체크아웃 → 샌드박스 테스트 → PR 코멘트, 동기화

- [ ] FastAPI `POST /webhook/github`: 서명 검증, 리포 허용 목록, delivery ID 중복 제거, 큐 등록, 202 응답
- [ ] `models/job.py` 작업 계약, RQ 워커 진입점, 작업 키 중복 방지
- [ ] preflight: gh·claude 로그인, Docker, Redis 확인
- [ ] bare clone 캐시 + worktree 생성·정리, 호스트에서 `changes.diff` 만들기
- [x] 샌드박스: 베이스 Dockerfile, 캐시 키(의존성 해시 + 이미지 digest + Python 버전)별 `.venv` 설치와 캐시, 테스트 컨테이너에는 `:ro` 마운트, `run_in_sandbox()`
- [ ] LangGraph 직선 그래프(체크아웃 → 테스트 → 코멘트). 체크포인터 없이 시작
- [x] worktree에서 `.venv` + pytest 직접 실행 (샌드박스 안), 결과 JSON과 커버리지 파일 남기기
- [ ] PR 코멘트 하나 달고 진행하면서 수정하기 ("테스트:" 줄, 환경 오류는 결과 대신 대기 표시)
- [ ] SQLite `runs`에 결과·상태·생존 확인 시각 저장
- [ ] 동기화(api 안의 루프): 열린 PR의 head 비교, 닫힌 PR 처리, 죽은 작업 재등록
- [ ] 최신 커밋 확인과 정리: 단계 사이, 그리고 `run_in_sandbox()` 실행 중 10초 간격 (실행 중 확인과 컨테이너 정리는 구현. Redis 최신 head 연결과 단계 사이 확인이 남음)

### 2단계. 컨벤션(lint): 검출 → 수정 → 제안 PR

메인 흐름(검출 → 승인 → 수정 → 검증 → 제안 PR)을 가장 단순한 스킬로 처음부터 끝까지 완성한다.

- [ ] `.pipeline.yaml`·도구 설정 파일을 base 브랜치에서 읽기, `sandbox.memory_gb`(상한 `max_memory_gb`)를 설치·작업 컨테이너에 적용하고 메모리 초과 코멘트에 설정 안내 추가
- [ ] 의존성 설치 실패 처리: 원인 분류에 따라 실행 불가·환경 오류, 판단 불가는 캐시 키별 연속 실패 횟수(Redis)
- [ ] `Finding` 인터페이스, ruff로 바뀐 줄의 위반 검출 (`changes.diff`)
- [ ] PR 코멘트: "테스트:" 줄과 "규칙:" 줄(⚠️/✅), 위반 목록과 출처, 제안 명령
- [ ] 명령 처리: `/recheck`, `/fix lint`, 꺼진 확장 명령에는 "꺼져 있음" 코멘트
- [ ] SqliteSaver 체크포인트, thread_id `repo#pr#head_sha`, `/fix lint` interrupt·재개 (재개 시 worktree 다시 만들기)
- [ ] 수정: `ruff check --fix`(안전한 수정만), 조건부 `ruff format` → 샌드박스 테스트 재실행 → 위반 0 확인
- [ ] 제안 PR: 허용 경로만 스테이징, 푸시 전 비밀값 검사, 머지 확인, 상황별 처리, 루프 방지
- [ ] flaky 재실행
- [ ] 평가 케이스: 컨벤션 위반 주입 3–5개와 E2E 테스트

### 3단계. 정적변수(static): 검출 → 안전망 → AI 수정

- [ ] 검출: ruff 규칙(매직 값, `global`) + 반복 리터럴·모듈 수준 가변 상태 (semgrep 또는 AST)
- [ ] 안전망: 위반 줄 커버리지(`coverage.xml`), 참고 지표로 호스트 diff-cover
- [ ] claude CLI 호출 (권한 제한, cwd 고정, `CLAUDE.md` 주입 여부 확인)
- [ ] 허용 범위 검사: `git status --porcelain --untracked-files=all`, 무시된 파일은 AI 실행 직전 스냅샷과 비교. 실패 경로 테스트: 새 파일 생성, 테스트 파일 삭제, `.github/` 수정, 허용 밖으로 이름 변경, 무시된 경로에 `conftest.py` 새로 생성. 통과 경로 테스트: 이미 있던 무시된 파일은 문제 삼지 않음
- [ ] 작성 → 검증 → 재시도 서브그래프 (최대 3회), 가벼운→고성능 라우팅
- [ ] PR당 예산·비용 기록
- [ ] 평가 케이스: 하드코딩 값·전역 상태 주입, 테스트가 지나가지 않는 위반, 고치면 테스트가 깨지는 위반

### 4단계. 파일 구조(structure): 규칙 읽기 → 검출 → AI 수정

- [ ] 구조 규칙 읽기: base의 import 그래프와 이름 패턴, README 변환과 해시 캐시, 출처 표시
- [ ] 검출: import 방향, 파일 위치·이름
- [ ] 자동 수정은 1–2순위 규칙과 import 방향 위반만, 이름 패턴은 알림만 (3단계 서브그래프 재사용)
- [ ] 읽어낸 규칙을 `.pipeline.yaml` 초안으로 제안
- [ ] 구조가 다른 두 번째 예제 리포, 구조 위반 주입 케이스

### 5단계. 벡터DB

- [ ] compose에 Chroma 추가, preflight 확인
- [ ] 기록 수집: 위반 → 채택된 수정, 거절된 제안, 검증에 실패한 수정
- [ ] 검색 결과를 수정 프롬프트에 넣기
- [ ] 평가 세트로 검색 유무 비교

### 6단계. 평가와 문서화

- [ ] 평가 케이스 20–30개로 확대, 평가 스크립트, 기준선 저장
- [ ] 모델 비교 실험 (가벼운 / 고성능 / 라우팅 / 가벼운 + 검색)
- [ ] Slack 운영 알림
- [ ] 실제 협업 리포 연결과 검증 (운영자가 진행)
- [ ] README: 구조, 설계 이유, 한계, 측정 결과, 데모 GIF

### 7단계. 선택 확장: 테스트 품질 (F13)

- [ ] 수상한 테스트 탐지
- [ ] 바뀐 줄 뮤테이션 (트리거, 상한·타임아웃, `/mutate`), 안전망 조건에 뮤턴트 처치 추가
- [ ] AI 테스트 생성과 테스트 제안 PR (`/generate-tests`, 3단계 서브그래프 재사용)
- [ ] pytest-testmon으로 영향 테스트만 실행, 시간 절감 측정

## 측정 지표

모든 지표는 SQLite의 `runs`·`proposals`·`eval_runs`에서 SQL로 뽑는다. 이력서 문장은 이 숫자로만 쓴다.

| 지표 | 정의 | 쓰임 |
| --- | --- | --- |
| 위반 검출 수 | PR당 스킬별 위반 수, 규칙 출처별 분포 | 어떤 규칙이 자주 어겨지는지 |
| 자동 수정 대상 비율 | 위반 중 안전망 조건을 충족한 비율 | 테스트가 안전망으로 얼마나 쓰이는지 |
| 위반 해소율 | 수정 루프가 종료 조건을 충족한 비율 (스킬별) | 수정 품질 |
| 검증 폐기율 | AI 수정안 중 테스트를 깨뜨리거나 새 위반을 만들어 버린 비율 | AI 결과물을 검증해야 하는 이유 |
| 수정 제안 채택률 | 수정 제안 PR 중 머지된 비율 | 수정안의 실용성 |
| 채택 수정 1개당 비용 | AI 비용 ÷ 채택 가능한 수정 수 | 모델 비교·라우팅 효과 |
| AI 호출 비율 | 전체 검사 중 AI를 호출한 비율 | 비용 최적화 효과 |
| 동기화 복구 수 | 웹훅 없이 동기화로 처리한 작업 수와 비율 | 로컬 서버 중단 대응 |
| 평가 세트 해소율 | 모델·조건별 결과 | 모델 비교, 검색 효과 |

이력서 문장 형식: "팀 규칙(README·파일 구조)에서 읽은 기준으로 PR 위반을 검출하고, AI 수정안 중 ○%를 테스트·재검출로 걸러내 채택률 ○%를 달성. 가벼운 모델 우선 라우팅으로 채택 수정당 비용을 ○% 줄임."

## 설계 결정 기록과 열린 질문

**결정 기록**

| 주제 | 결정 | 이유 | 버린 대안 |
| --- | --- | --- | --- |
| 기준 단위 | PR | 협업에서 실제로 확인하는 단위 | 커밋 단위 요약·테스트 (과함) |
| 주 기능 | 협업 규칙(컨벤션·정적변수·구조)에 맞춘 수정 제안 | 리뷰에서 반복되는 규칙 지적을 자동화. 규칙을 리포에서 읽는 점이 차별점 | 테스트 품질 판정을 메인으로 (기본 테스트 자동화는 CI 영역), AI가 판정하는 코드 리뷰 (결과가 매번 다름) |
| 테스트의 역할 | 수정안의 안전망 (기준선, 위반 줄 커버리지, 수정 후 재실행) | 자동 수정이 동작을 깨지 않는다는 근거 | 뮤테이션·AI 테스트 생성까지 기본 포함 (7단계 선택 확장으로) |
| 트리거 | 웹훅 + PR 상태 동기화 | 로컬 서버는 꺼질 수 있고 GitHub은 웹훅을 다시 보내지 않음 | 웹훅만 (누락), 폴링만 (느림) |
| 작업의 의미 | PR을 현재 상태에 맞추기 | 웹훅과 동기화가 같은 경로를 탐 | 이벤트 종류별 처리 |
| 배치 도구 | 없음 | 이벤트 + 주기 동기화로 충분 | Airflow (리포가 늘면 재검토) |
| 워커 위치 | 로컬 프로세스 | claude 로그인 재사용, docker.sock 마운트 불필요 | 컨테이너 워커 |
| 워커 분리 | 처음부터 별도 프로세스 | 샌드박스 테스트와 AI 수정 루프가 길고 무거움 | BackgroundTasks |
| 큐 | RQ | 작업이 subprocess 위주라 동기식이 맞음 | arq, Celery |
| 작업 취소 | 단계 사이와 실행 중(10초 간격)에 워커가 최신 커밋 확인 | 강제 종료는 컨테이너와 worktree를 남김 | 작업 강제 종료 |
| 샌드박스 도입 시점 | 1단계부터 | PR 코드가 호스트에서 실행되지 않음, 실행 함수를 두 번 만들지 않음 | AI 수정 단계에 도입 |
| 작업 흐름 도구 | 1단계부터 LangGraph | 같은 모양의 수정 루프가 셋(lint·static·structure), 도입이 확정이라 재구성 비용을 없앰 | 수정 단계에 도입해 재구성 |
| 영구 저장 | SQLite | 로컬 도구, 서버 불필요, 지표 조회 | Redis만 사용 |
| 벡터 저장소 | Chroma (Docker) | 원본은 SQLite에 두어 교체 가능. 5단계에서 검색 유무를 평가 세트로 비교해 유지 여부를 정한다 | sqlite-vec |
| AI 역할 | 수정안 작성, README 규칙 변환 | 판정은 결정적이어야 함 | AI가 통과·위반 여부 판단 |
| 제안 반영 | 제안 PR (스킬별) | 작성자 브랜치를 건드리지 않음 | PR 브랜치에 직접 커밋 |
| 구조 검증 기준 | 대상 리포의 README와 파일 구조 | 리포마다 구조가 다름 | MVC 등 특정 패턴 고정 |
| 설정·규칙을 읽는 곳 | base 브랜치 | PR이 기준을 바꿔 자기 검사를 통과시키지 못하게 함 | PR head |
| 스킬 구현 순서 | lint → static → structure | 도구만으로 고치는 lint로 메인 흐름을 먼저 완성하고, AI 수정은 범위가 좁은 static부터 | structure 먼저 |
| 영향 테스트 실행 | 7단계로 미룸 | 기준 데이터 캐시가 필요하고 정확성이 먼저 | 1단계에 도입 |
| 실제 리포 검증 | 운영자가 협업하면서 나중에 | 구현 중에는 예제 리포로 재현 가능한 테스트가 필요 | 처음부터 실제 리포에 연결 |
| Slack 역할 | 운영 오류 알림 | 협업 결과는 PR에 있어야 함 | PR 결과 전체 전달 |
| 비밀값 | 로컬 로그인 + 파일 | 환경변수는 자식 프로세스에 상속됨 | 환경변수, 코드에 직접 |

**열린 질문**

- [x] mutmut이 바뀐 줄 한정과 뮤턴트 지정 실행을 지원하는지: 지정 실행은 지원, 바뀐 줄 한정은 직접 구현 (F13)
- [x] claude CLI의 로그인 확인 명령, 경로 제한, 프로젝트 설정 차단 방법: `claude auth status`, `--restricted` (보안·인증)
- [ ] 안전망 기준: 위반 줄 커버만 볼지, 위반 줄이 속한 함수 전체 커버를 요구할지 (3단계 평가 후 결정)
- [ ] 가벼운·고성능 모델을 구체적으로 무엇으로 할지, 로컬 모델을 포함할지
- [ ] 반복 리터럴·모듈 수준 가변 상태 검출을 semgrep으로 할지 AST 검사로 직접 쓸지
- [ ] 임베딩 모델
- [ ] 실제 협업 리포와 연결 시점
- [ ] PR 화면 Check Run을 위해 GitHub App을 언제 만들지

**범위 밖 확장 아이디어:** 운영 중 에러 → Slack 알림 → 재현 테스트 자동 생성 → 수정 PR. 다른 사용자(운영자)를 위한 별개 흐름이라 이번에는 다루지 않는다.
