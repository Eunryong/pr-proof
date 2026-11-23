## 12. 기존 코드와의 대응표

### 12.1 모듈별

이 표의 모듈 칸은 v1의 이름이고, 경로는 2026-11-21에 폴더를 나누기 전(`app/` 아래)의 것이다. 지금 자리는 12.5의 표로 읽는다.

| 모듈 | 구분 | v2에서 |
| --- | --- | --- |
| `api.py`, `cli.py`, `config.py` | 그대로 | `cli`에 `reindex`를 더했다 (5단계, 2026-11-20). `config`에 `rag.embed_model` |
| `controllers/`, `services/`, `views/` | 그대로 | 수신·동기화·큐 (4.1) |
| `models/job.py`, `state_keys.py`, `sync_rules.py`, `install.py`, `comment.py` | 그대로 | |
| `models/test_run.py`, `diff.py`, `text_edit.py`, `coverage.py`, `scope.py` | 그대로 | 테스트 결과, 바뀐 줄, 편집 적용, 커버 여부, 허용 범위 규칙 |
| `sandbox/container.py`, `safe_read.py` | 그대로 | 4.5, 8.1. `container.py`는 언어 프로필이 넘기는 읽기 전용 마운트(.venv, 런타임)를 붙인다 (3단계) |
| `worker/main.py`, `deps.py`, `retry.py`, `repo.py`, `github.py`(코멘트 부분), `state.py`, `preflight.py`, `coverage.py`(diff-cover만), `scope.py` | 그대로 | 체크아웃, 재시도, 기록 쓰기, 범위 검사 실행 |
| `models/finding.py` | 바뀜 | `Finding` → `Violation`: `skill`이 `rule_id`·`found_by`로 (7.3) |
| `models/repo_config.py` | 바뀜 | 10절의 스키마 |
| `models/command.py` | 바뀜 | `/fix`의 인자가 스킬에서 묶음으로. 확장 명령 삭제 |
| `models/fix.py` | 바뀜 | `FixOutcome`이 묶음 단위로, 판단 함수는 4.6·4.8로 |
| `models/proposal.py` | 바뀜 | 브랜치 이름, 본문(요약·검증 절), 비밀값 검사 대상에 줄 리뷰 코멘트 |
| `models/rejection.py` | 바뀜 | 키의 "규칙 코드"가 규칙 id로. 논리는 그대로 |
| `models/ai_fix.py` | 없앰 (2단계) | 모델 고르기와 프롬프트는 `models/model_fix.py`로, 달러 예산은 호출 수 상한(`models/call_budget.py`)으로 바뀌었다. 호출 하나의 `--max-budget-usd`만 `call_budget.CALL_BUDGET_USD`에 남았다 |
| `models/db.py` | 바뀜 | 7.8의 새 테이블 |
| `worker/graph.py` | 바뀜 | 5.1의 그래프로 다시 짠다. 체크포인터, thread_id, 낡은 커밋 확인, 재개는 그대로 |
| `worker/ai_fix.py` | 없앰 (2단계) | 루프는 공용 틀의 `write`·`verify`(`worker/model_fix.py`)가 되었다. `AIEnvironmentError`와 `environment_problem`은 `worker/ai/claude_cli.py`로 옮겼다 |
| `worker/skills/lint.py` | 없앰 (3단계, 2026-11-20) | 검사는 도구 설정의 규칙 묶음과 `worker/detect.py`로, 수정은 `worker/mechanical_fix.py`로 옮겼다. 안전한 수정만 바뀐 줄에 적용하는 논리는 그대로다. `models/finding.py`, `models/rejection.py`, `report.py`의 v1 절도 같이 없앴다 |
| `worker/skills/static.py`, `static_rules.py` | 없앰 (2단계, 2026-11-20) | 파이프라인 내장 검사기를 두지 않는다 (2.2) |
| `worker/base_config.py` | 바뀜 | 규칙 문서도 base에서 읽는다 |
| `worker/report.py` | 바뀜 | 읽은 규칙, 수정 판단의 이유, 검사하지 못한 규칙 |
| `worker/proposal.py` | 바뀜 | 묶음 단위, 줄 리뷰 코멘트, 상태 확인 때 기록 남기기. 상태 처리는 그대로 |
| `worker/command.py`, `update.py` | 바뀜 | `/fix` 형식, 시도 세대, 작성자가 직접 고친 위반 알아내기 |
| `worker/github.py` | 바뀜 | 리뷰(줄 코멘트) 쓰기, 그 브랜치의 열린 PR 찾기 |
| `worker/ai/claude_cli.py` | 바뀜 | 허용 도구와 시스템 프롬프트를 인자로. 도구 없는 호출. 2026-11-22에 수정 작성의 모양 `fix_tools`(파일 도구 다섯과 파이프라인 도구 둘, `--mcp-config`)를 더했다. 도구 서버는 `worker/ai/tool_server.py`, CLI가 띄우는 중계는 `worker/ai/mcp_relay.py`, 메시지와 상한은 `worker/mcp_pure.py`, 만든 테스트의 판정은 `worker/generated_tests_pure.py`, 표시 묶기는 `worker/test_lines.py` |
| `worker/ai/model_backend.py`, `claude_backend.py`, `backends.py` | 새로 (2026-11-23, pr-proof#87) | 모델 호출의 인터페이스 `ModelBackend`(`ask`, `reading`, `fix`, `startup_check`, 능력)와 구현마다 지킬 조건(8.4), 결과·오류 타입(`EditResult`, `StructuredResult`, `AIErrorKind`, `AIEnvironmentError`. `claude_cli`가 옛 이름 `ClaudeResult` 등을 다시 내보낸다). 첫 구현 `ClaudeCliBackend`는 `claude_cli.py`(호출 모양 넷)와 `workdir.py`의 `reading_folder`·`run_in_model_folder`(훅)를 그대로 잇고 claude 로그인 확인을 맡는다. `backends.make_backend`가 이름으로 구현을 만든다. 호출하는 쪽(`rules/`, `fix/`, `feedback.py`, `graph.py`, `preflight.py`)은 `claude_cli`·`workdir`의 호출을 부르지 않는다. `workdir.py`의 폴더 만들기·복사·`path_digest`는 구현이 같이 쓴다 |
| `docker/sandbox/Dockerfile` | 바뀜 (3단계, 2026-11-20) | ruff와 Python을 빼고 언어와 무관한 최소 이미지(`debian:bookworm-slim`에 curl과 인증서)로 |
| `sandbox/pytest_runner.py`, `ruff_runner.py`, `venv.py`, `worker/coverage.py`(위반 줄의 커버리지 읽기) | 옮김 (3단계, 2026-11-20) | Python 프로필 `worker/lang/python.py`로 (7.7). ruff의 `check`·`rule_exists`는 `ruff_check`·`ruff_rule_exists`로 이름만 바꿨다. `.venv`는 도구 캐시의 런타임으로 만들고 캐시 키에 이미지의 Python 버전 대신 런타임 버전이 든다 |
| `sandbox/docker_cli.py`, `toolcache.py` | 바뀜 (3단계, 2026-11-20) | `TOOL_IMAGE`(`python:3.11-slim`)를 없애고 모든 컨테이너가 샌드박스 이미지 하나를 쓴다. 도구 캐시에 Python 런타임(폴더째 푸는 도구)과 curl로 받는 받기 컨테이너 |
| `docker-compose.yml` | 그대로 | Redis만. 벡터 저장소는 Chroma 임베디드라 컨테이너를 더하지 않는다 |
| `models/rule.py`, `checker.py`, `decision.py`, `explain.py`, `record.py`, `cache_key.py` | 새로 | 7절 |
| `worker/rules.py` | 새로 | 규칙 읽기 (4.3) |
| `worker/methods.py`, `worker/tools.py` | 새로 → 다른 이름으로 | 검사 방법 정하기는 `worker/checkers.py`(도구 선택과 검사기 작성), 도구 목록과 ruff 설정·버전 읽기는 `models/tools.py`, 도구 선택의 결정적 부분은 `models/tool_choice.py`, 기계적 수정은 `worker/mechanical_fix.py` (3단계) |
| `worker/loop.py` | 새로 | 공용 서브그래프 틀 (5.3) |
| `worker/checker_author.py`, `sandbox/checker_runner.py` | 새로 | 검사기 작성과 샌드박스 실행 |
| `worker/judge.py` | 새로 | 모델의 직접 판단 (4.5) |
| `worker/decide.py` | 새로 | 수정 판단 (4.6) |
| `worker/model_cache.py` | 새로 | 해시 키 캐시 (5.6) |
| `models/retrieval.py` | 새로 | 기록 텍스트, 필터와 기준값, 프롬프트 블록 (6.6의 1·6·7번. 외부 호출 없음) |
| `worker/memory.py` | 있음 (5단계, 2026-11-20) | 임베딩, Chroma 저장과 top-k 검색 (6.6의 2·3·5번), 밀린 기록 임베딩, `reindex`, 큰 규칙 문서의 조각 검색. 기록 저장(SQLite)은 2단계의 `worker/records.py`. 1·4·6·7번은 `models/rag.py`, 큰 문서의 조각은 `models/doc_sections.py`. 검색을 켤지(`switch_for`, 선택 의존성 찾기 `missing_extras`)도 여기 있고 판단 규칙은 `models/rag_switch.py`다 (6.7, 2026-11-20) |
| `sandbox/toolcache.py` | 새로 | 도구를 필요할 때 받아 캐시 (2.3, 4.4) |
| `worker/lang/python.py` | 새로 | Python 프로필. 지금 흩어져 있는 Python 전용 코드를 모은다 (7.7) |

