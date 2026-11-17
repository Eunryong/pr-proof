# CLAUDE.md

이 리포는 PR 테스트 검증 파이프라인이다. 전체 설계는 `docs/DESIGN.md`에 있고, 작업 전에 해당 섹션을 먼저 읽는다.

## 지켜야 할 원칙

- 판정(통과·실패, 점수)은 결정적 도구(pytest, diff-cover, mutmut 등)만 낸다. AI 호출 결과로 판정하지 않는다.
- api(FastAPI), sync, worker는 메모리를 공유하지 않는다. 넘길 값은 `app/jobs.py`의 `PRJob`으로만, 상태는 Redis(임시)·SQLite(영구)에 둔다.
- PR에서 온 코드와 AI가 쓴 코드는 Docker 샌드박스에서만 실행한다. 호스트에서 대상 리포의 pytest를 돌리지 않는다.
- 대상 리포의 설정과 규칙은 PR head가 아니라 base 브랜치에서 읽는다.
- 비밀값은 코드·환경변수에 넣지 않는다. `app/config.py`의 파일 읽기 함수로만 접근한다.
- subprocess에는 최소 환경만 넘긴다.
- 기능은 `docs/DESIGN.md`의 단계 순서대로 만들고, 각 단계는 동작하는 상태로 끝낸다. 다음 단계의 구조(플러그인 로더, Chroma, 체크포인터 등)를 미리 만들지 않는다.

## 현재 단계

- 0단계 완료. 1단계 (수신 → 샌드박스 테스트 → PR 코멘트, PR 상태 동기화) 진행 중. 완료하면 `docs/DESIGN.md`의 체크리스트를 갱신한다.
- 작업은 기능 단위 브랜치와 PR로 진행한다. 이 리포(`pr-proof`)가 파이프라인의 첫 대상 리포이고, 1단계가 동작하면 파이프라인이 자기 PR을 검사한다.
- 실제 협업 리포 검증은 나중에 한다. 각 단계는 `eval/`의 예제 리포와 저장해 둔 웹훅 페이로드로 검증한다.

## 실행

```
docker compose up -d  # redis
honcho start          # api, worker, sync
pytest                # 파이프라인 자체 테스트
```

워커 실행 전 `gh auth login`, `claude` 로그인, Docker 실행이 필요하다. preflight가 확인한다.

## 코드 스타일

- Python 3.11+, 타입 힌트 필수, ruff로 포맷·린트
- 새 기능에는 단위 테스트를 같이 작성한다. 테스트는 경계값과 실패 경로를 포함한다.
