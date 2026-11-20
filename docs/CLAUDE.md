# CLAUDE.md

이 리포는 팀의 규칙을 그 저장소의 문서에서 읽고, PR이 바꾼 코드에서 어긋난 곳을 찾아, 테스트로 확인한 수정안을 별도 PR로 올리는 파이프라인이다. 사람은 수정안 PR을 머지하거나 닫기만 한다. 테스트 실행은 수정안의 안전망이다. 전체 설계는 `docs/DESIGN.md`(v2)에 있고, 작업 전에 해당 섹션을 먼저 읽는다. `docs/DESIGN.v1.md`는 예전 설계의 보관본이고, v2가 "v1 F○와 같다"로 가리키는 세부 규칙만 거기서 읽는다.

## 지켜야 할 원칙

- 판단은 모델이, 검증은 결정적 도구가, 머지는 사람이 한다. 결정은 네 종류다(DESIGN 2.1): 규칙 선택(모델), 위반 판단(도구가 있으면 도구, 없으면 모델), 수정 판단(모델), 수정 검증(결정적 도구만). 수정 검증에는 모델을 쓰지 않는다.
- 모델의 결과는 입력 해시별로 저장하고 다시 쓴다. 재현성은 모델을 금지해서가 아니라 저장해서 보장한다.
- 규칙은 대상 리포의 문서와 도구 설정에서만 나온다. 파이프라인은 내장 규칙도 내장 검사기도 갖지 않는다. 특정 구조(MVC 등)를 강제하지 않는다.
- 도구는 미리 깔지 않고 규칙 계획에 필요할 때만 받아 캐시한다(DESIGN 2.3).
- 웹(api)과 worker는 메모리를 공유하지 않는다. 넘길 값은 `app/models/job.py`의 `PRJob`으로만, 상태는 Redis(임시)·SQLite(영구)에 둔다.
- PR에서 온 코드와 모델이 쓴 코드(수정안, 검사기)는 Docker 샌드박스에서만 실행한다. 호스트에서 대상 리포의 pytest를 돌리지 않는다.
- 대상 리포의 설정과 규칙은 PR head가 아니라 base 브랜치에서 읽는다.
- 비밀값은 코드·환경변수에 넣지 않는다. `app/config.py`의 파일 읽기 함수로만 접근한다.
- subprocess에는 최소 환경만 넘긴다.
- 기능은 `docs/DESIGN.md`의 단계 순서대로 만들고, 각 단계는 동작하는 상태로 끝낸다. 다음 단계의 구조(플러그인 로더, Chroma, 체크포인터 등)를 미리 만들지 않는다.

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

## 현재 단계

- 코드는 v1 구조(수신·체크아웃·샌드박스 테스트·코멘트·동기화)에 v2의 규칙 경로(규칙 읽기, 검사 방법 정하기, 위반 찾기, 수정 판단, 기계적·모델 수정, 수정안 PR, 기록)를 더한 것이다. v1의 static(파이프라인 내장 검사기)은 v2 2단계에, v1의 lint 고정 단계는 v2 3단계에 지웠다. v1의 4단계(structure) 이후는 하지 않는다.
- 2026-11-18에 설계를 v2로 다시 썼다. v2의 0단계(문서)와 1단계(규칙 읽기와 검사기 작성, 도구 없이. 2026-11-19)가 끝났고, 1단계 직후 묶음(경로 패턴 검사기, 범위 추정의 효과 확인, 확인 대상 고르기. 1a-1~1a-4)도 2026-11-19에 끝났다. 그 뒤 2단계를 진행했다(아래). 순서는 `docs/DESIGN.md` 13절을 따른다: 1 규칙 읽기와 검사기 작성(호출 상한 포함) → 1단계 직후(경로 패턴 검사기, 범위 추정의 효과 확인) → 2 수정 판단과 수정 연결, 내장 검사기 지우기 → 3 저장소의 도구 연결 → 4 모델의 직접 판단 → 5 RAG 검색 → 6 평가 확대와 운영. 완료하면 `docs/DESIGN.md`의 체크리스트를 갱신한다.
- 2단계의 첫 작업으로 수정 판단(DESIGN 4.6)이 테스트 뒤에 돈다(2026-11-19). 문서 규칙 위반마다 fix/skip과 이유를 정해 코멘트에 보인다. 모델 답은 위반마다 `model_cache`에 저장한다.
- 2단계의 두 번째 작업으로 `fix`·`model`로 정한 문서 규칙 위반을 고친다(2026-11-20, `worker/model_fix.py`). 공용 틀(`worker/loop.py`)의 write가 모델 전용 폴더에서 파일 도구만 준 모델을 부르고(지시는 `--append-system-prompt`), verify가 허용 범위·파이프라인 파일·비밀값·설명 JSON·규칙 재검사·샌드박스 테스트·크기 상한을 결정적으로 본다. 통과하면 수정안 PR(`pipeline/pr-<n>-fix-model`)을 연다. 호출은 PR 상한에서 센다. v1 lint·static 경로는 그대로다.
- 2단계의 세 번째 작업으로 전달을 다시 할 수 있게 하고 기록을 남기기 시작했다(2026-11-20). 문서 규칙 경로의 수정 시도는 `fix_attempts`에 저장되고, `deliver_model`은 늘 새 worktree에 저장한 패치를 적용해 파일 해시를 맞춰 본 뒤 올린다. RQ가 작업을 다시 시도하면 `deliver_model`에서 멈춘 thread를 그 node부터 이어 가므로 모델 호출 기록이 늘지 않는다. `violations`와 `records`(채택·거절·실패·작성자 수정, `worker/records.py`)를 저장 전 비밀값 검사를 거쳐 남기고, 같은 PR의 거절 기록이 다음 커밋의 같은 줄을 `skip`(이전에 거절됨)으로 만든다. 검색은 5단계다.
- 2단계의 네 번째 작업으로 수정 묶음과 `/fix`를 맞췄다(2026-11-20). `/fix`의 인자는 묶음이다: `/fix model`(문서 규칙 경로), `/fix mechanical`(3단계까지 v1 lint와 같다), 인자 없는 `/fix`(할 일이 남은 묶음 전부). `/fix lint`도 아직 받는다. `require_approval: true`면 모델 묶음이 승인을 기다리고 `/fix model`이 그 thread를 잇는다. 호출 상한에 걸려 남은 수정 판단과 수정은 `/fix`가 PR 상한을 한 번 더 받아(`call_grants`) 수정 판단부터 새 시도 세대로 이어 한다. 수정안 PR에는 설명 JSON의 `changes`마다 줄 리뷰 코멘트를 리뷰 하나로 단다. `.pipeline.yaml`의 `fix.*`는 최상위 키이고 (옛 `skills.fix`도 2단계 동안 읽는다) `max_retries`는 5로 자른다. 확장 명령(`/mutate`, `/generate-tests`)과 `extensions.*` 설정을 지웠다.
- 2단계의 다섯 번째 작업으로 내장 검사기(static)와 v1 AI 수정 루프, 달러 예산(`model.budget_per_pr_usd`)을 지웠다(2026-11-20). 파이프라인이 정한 규칙은 ruff 설정이 없는 base의 ruff 기본 규칙(v1 lint, 3단계에서 없앰)뿐이다. `/fix static`은 지원하지 않는 명령이다. `.pipeline.yaml`의 `skills.static`, `model.budget_per_pr_usd`, `skills.enabled`의 `static`·`structure`는 무시하고 "설정:"에 알린다. `eval/static`은 `eval/rules/09`–`12`, `15`, `16`으로 옮겼다. 검사기 작성 프롬프트가 규칙이 덮는 코드 모양을 모두 위반 예시로 나누게 바뀌어, 저장소마다 검사기를 한 번 다시 쓴다.
- 3단계(저장소의 도구 연결)를 시작했다(2026-11-20, pr-proof#33). 첫 작업으로 ruff를 새 구조로 옮기고 v1 lint 단계를 지웠다. base의 ruff 설정(`ruff.toml`, `.ruff.toml`, `pyproject.toml`의 `[tool.ruff]`)은 도구 설정의 규칙 묶음(규칙 id `ruff`, 위반의 규칙 id는 ruff 코드)이 된다. 문서 규칙은 검사기 작성 전에 모델이 도구 목록(`ruff`, `ruff-format`)에서 맞는 도구를 고르고, 고른 코드는 `ruff rule`과 예시로 확인한다. ruff는 이미지에서 빠졌고, 규칙이 쓸 때만 저장소가 고정한 버전(없으면 0.17.5)을 해시를 확인해 도구 캐시에 받는다. ruff 설정도 ruff를 쓰는 규칙도 없으면 ruff를 받지 않고, 규칙이 하나도 없으면 "규칙을 찾지 못해 규칙 검사를 하지 않음"을 적는다. 안전한 수정이 있는 위반은 기계적 수정 묶음(`worker/mechanical_fix.py`, 수정안 PR `pipeline/pr-<n>-fix-mechanical`)이 고친다. `/fix lint`는 지원하지 않는 명령이 되었고 `skills.enabled`에 lint가 없는 옛 설정은 도구 설정의 규칙을 끈다. 완료 기준 3-1–3-5를 채웠다. 3-9는 저장소 주인에게 물었다(DESIGN 13절). 2026-11-20에 주인이 3-9를 "같은 ruff 규칙이면 같은 목록"으로 고쳐 기존 측정으로 채웠다.
- 3단계의 두 번째 작업으로 샌드박스 이미지를 언어와 무관한 최소 이미지로 바꾸고 Python 전용 코드를 `worker/lang/python.py`로 모았다(2026-11-20, 완료 기준 3-6). 이미지(`docker/sandbox/Dockerfile`)는 `debian:bookworm-slim`에 curl과 인증서뿐이고, 받기·설치·검사기·ruff·테스트 컨테이너가 모두 이 이미지다. Python 런타임은 python-build-standalone 3.11.18을 도구 캐시에 해시로 고정해 테스트나 Python 검사기가 쓸 때 받는다. 테스트 컨테이너에는 `/opt/python`에 읽기 전용으로 붙이고, 설치 컨테이너에는 복사해 넣는다. `.venv` 캐시 키에는 런타임 버전이 든다. 도구 받기는 이미지의 curl이 한다(https만, 크기 상한). 남은 3단계 작업은 도구 켬·끔 평가(3-7, 3-8)와 3-9(저장소 주인의 답)다.
- 3단계의 세 번째 작업으로 도구 켬·끔 평가를 넣고 실제 모델로 쟀다(2026-11-20, 완료 기준 3-7). `tests/e2e/test_tools_off_eval.py`가 `eval/tools_off.yaml`의 표본(1단계 기준선과 같은 eval 케이스 열 개와 probe, 환경변수로 주면 MathLLM base 전체)을 같은 규칙 읽기 결과로 도구 끔(`plan_and_author(ruff=None)`)과 도구 켬(base의 ruff)에서 돌려, 샌드박스 ruff를 정답지로 재현율·정밀도를 낸다. 기본은 가짜 모델이고 `PR_PROOF_REAL_MODEL=1`이면 실제 모델이다. 3-8은 채우지 못했다: 재현율은 기준선 이상이지만 정밀도가 낮은 칸이 있고(eval `PLW0603`의 README가 막는 줄, MathLLM 한 회의 `0`·`1` 비교), 정의를 어떻게 읽을지 저장소 주인에게 물었다(BACKLOG). 3단계는 3-8이 남아 끝나지 않았다(3-9는 위).
- 4단계(모델의 직접 판단)를 시작했다(2026-11-20, `v2/stage4-judge`). 검사기 작성이 세 번 실패한 규칙(경로 규칙은 빼고)은 모델 판단(`RuleJudged`)으로 내려오고, 그래프의 `judge` node가 `detect` 다음, 테스트 전에 바뀐 hunk(앞뒤 20줄)를 파일 단위로 도구 없이 묻는다(`worker/judge.py`, `model.judge` 기본 sonnet). 답은 (규칙, hunk) 쌍마다 `model_cache`에 저장하고, 줄이 그 hunk에서 PR이 바꾼 줄이고 인용이 그 줄과 같은 답만 위반(`found_by: model`)으로 남긴다. 이 위반은 수정 판단과 모델 수정을 그대로 타고, 검증은 "그 줄의 내용이 바뀌었다"까지만 보며 수정안 PR 본문에 "도구로 재검사하지 못함"이 적힌다. PR 상한은 `min(pr_base + 대상 위반 수 × per_violation, pr_max) + pr_max × /fix 횟수`다. 호출 상한으로 남은 모델 판단은 코멘트에 규칙 수가 나오고 `/fix`가 모델 판단부터(테스트도 다시) 이어 한다. 평가 케이스는 `eval/judge/`, 하네스는 `tests/e2e/test_judge_cases.py`다. 완료 기준 4-8·4-9·4-10과 PR 단위 확인(4.5 끝)은 남았다. PR 단위 확인의 완료 기준은 2026-11-20에 저장소 주인이 4-11–4-14로 정했고 기능은 아직 없다.
- 4단계의 측정 작업으로 4-8을 채웠다(2026-11-20, `v2/stage4-measure`). 도구를 끈 평가(`tests/e2e/test_tools_off_eval.py`)가 "모델 판단만" 조건과 판단 케이스(`eval/judge/01`, `06`, 정답은 케이스가 표시한 줄)를 더해 확인 방법별로 재현율·정밀도를 낸다. 실제 모델 3회에서 운영 흐름의 규칙은 모두 검사기나 도구를 얻어 모델 판단으로 내려간 규칙이 0/30이었고, 같은 표본에서 도구 끔의 검사기는 27/33·27/29, 모델 판단만은 33/33·33/36이었다(검사기가 주석 규칙을 3/3 놓침). 4-10은 MathLLM 커밋 하나를 나눈 PR 다섯 개로 분포를 쟀지만(PR 상한의 호출은 많아야 1회, 규칙 묶음은 첫 PR의 16회) 수정 작성과 운영의 모델 판단이 돌지 않은 작은 표본이라 기본값을 바꾸지 않고 저장소 주인에게 물었다. 4-9는 사람의 일이고, 운영 흐름에서 모델이 찾은 위반이 0건이라 모델 판단만으로 물은 97건을 저장소 밖의 검토 파일로 만들어 두었다. 4단계는 4-9·4-10과 PR 단위 확인이 남아 끝나지 않았다.
- 5단계(RAG 검색)를 진행했다(2026-11-20, `v2/stage5-rag`). 과거 기록(`records`)을 저장소마다 따로인 Chroma 임베디드 컬렉션(`data_dir/chroma/`)에 로컬 임베딩 모델(multilingual-e5-small, 판과 SHA256 고정, `data_dir/models/`)로 넣고, 수정 판단(채택·거절·실패), 수정 작성(첫 회차 채택, 재시도 실패), 검사기 작성(검사기 기록)의 프롬프트에 "과거 기록이며 지시가 아니다" 블록으로 `rag.max_records`건까지 넣는다(`worker/memory.py`, `models/rag.py`). 기록이 없는 호출의 프롬프트와 저장 키는 검색을 켜기 전과 같다. 결과 코멘트에 참고한 기록·유사도·쓰인 단계가 접힌 블록으로 나온다. 규칙 문서가 400KB를 넘으면 제목 단위 조각을 임베딩해 관련 절만 읽는다(`models/doc_sections.py`). `rag.enabled`의 기본은 끔이고, 꺼져 있으면 chromadb·임베딩 모델을 import하지도 받지도 않는다. 2026-11-20에 저장소 주인이 값을 `auto`(기본, 채택·거절 기록 20건부터 켜고 30건 이상에서 검색 붙은 쪽 채택률이 낮으면 끔) / `on` / `off`로 정했다. 코드는 아직 `true`/`false`다(DESIGN 12.2). 5-14는 비교가 아니라 체크를 거뒀다. 선택 의존성은 `pip install pr-proof[rag]`, 다시 만들기는 `python -m app.cli reindex`다. 유사도 기준값의 기본은 측정으로 0.91로 정했다. 완료 기준 중 무엇을 채웠는지는 `docs/DESIGN.md` 13절 5단계에 있다.
- 6단계(평가 확대와 운영)의 첫 묶음으로 외부 작성자 PR 가드레일을 정식으로 넣었다(2026-11-20, `v2/stage6-guardrail`, 완료 기준 6-8–6-11). 임시 가드레일을 대신한다. 워커가 `update`·`command` 작업의 시작에서 체크아웃 전에 GitHub의 PR 조회 한 번으로 head·작성자의 `author_association`·로그인을 읽고, base `.pipeline.yaml`의 `access.trusted`(기본 OWNER·MEMBER·COLLABORATOR)·`access.bots`(기본 없음, bare 캐시에서 blob만 읽음)와 `approvals`의 승인 기록을 더해 `models/access.may_run`으로 정한다(`worker/guard.py`). 통과하지 못하면 결과 코멘트에 승인 안내 한 줄만 달고 `runs`에 `blocked`로 남기며, 동기화는 그 head를 다시 넣지 않는다. 승인은 신뢰된 사람이 지금 head를 적어 단 `/recheck <커밋>`(전체 해시 16진수 40자. 2026-11-20에 저장소 주인이 7자 이상에서 바꿨다, T31)뿐이고, 워커가 그 코멘트를 GitHub에서 다시 읽어 승인자와 커밋을 확인한다(`worker/command.py`의 `_approve`). `[bot]` 계정은 승인하지 못한다. `PRJob`의 작성자 값은 기록용이다. 새 위협 T31(짧은 해시의 앞자리 맞추기)·T32(안내 코멘트와 GitHub 한도)의 시점은 2026-11-20에 저장소 주인이 `공개 전`으로 정했다. T31은 40자 승인으로 막았고 T32는 작업 수 상한이 없다. 평가와 운영(6-1–6-7)은 남았다.
- 6단계의 두 번째 묶음으로 평가와 운영을 넣었다(2026-11-20, `v2/stage6-eval-ops`). `python -m app.cli eval [--baseline] [--real-model]`이 `tests/e2e`의 docker 테스트 전부(케이스 파일, 도구를 끈 평가, 비교 하네스)를 돌려 `eval_runs`에 남기고 같은 종류의 가장 최근 기준선과 비교한다(`worker/evaluation.py`, `models/eval_run.py`). 케이스는 55개이고 팀 고유 규칙 케이스는 `eval/rules/`의 30개다(17–30을 더함). Slack 운영 알림은 웹과 워커가 같이 쓰는 계층 `app/notify`가 보낸다. URL은 비밀값 파일 `slack_webhook_url`에서만 읽고, 같은 (종류, 저장소)는 30분에 한 번이다(`models/ops_alert.py`, 위협 T33). 기반 이미지는 워커 프로세스의 첫 빌드와 그 뒤 하루에 한 번 `--pull`로 받는다. 기계적 수정의 재검사는 정렬이 자리만 옮긴 base의 줄을 PR의 줄로 세지 않는다. 수정 작성의 모델 세 구성 비교는 `tests/e2e/test_model_compare.py`다. 루트 conftest가 모든 테스트의 설정 폴더를 빈 임시 폴더로 정한다. 6단계는 끝나지 않았다. 채운 기준과 남은 기준은 `docs/DESIGN.md` 13절 6단계에 있다. 2026-11-20에 저장소 주인이 6-1·6-3의 체크를 보류했다(실제 모델 기준선을 만든 뒤 채운다). 6-2는 지금 구성(가벼운 다음 무거운)을 유지하고 무거운 모델로 올라가는 케이스를 만들어 다시 잴 때까지 열려 있다. `.pipeline.yaml`의 `notify.slack.ops`는 지웠다(옛 키는 무시).
- 2026-11-20에 저장소 주인이 열린 물음에 답했고(`v2/decisions-1120`) DESIGN 11.1의 "완료 기준을 고친 기록"에 줄마다 남겼다. 위협 표의 T29–T33은 `공개 전`이고, 주인은 팀 저장소가 공개라 `공개 전`이 지금이라고 했다(조정자가 `gh`로 본 `Pig30nidaE/BooBug`는 PRIVATE였고 두 사실을 같이 적었다). T7·T8·T25에 처리하지 않는 이유나 처리 방법을 적었다. 6-5는 T8의 `# noqa`·테스트 설정 알림과 T32가 남아 채우지 못했다. 의미 규칙의 모델 판단 기준("확인 불가/일부" 표시 또는 예시 확인 두 번 실패, 4.4 끝)은 문서에만 있다.
- 2단계의 완료 기준은 모두 채워졌다. 2026-11-20에 저장소 주인이 2-1을 "01은 수정안 PR까지 가고, 02는 잡히고 이유와 함께 `skip`된다"로 고쳤고 아래 닫기 작업의 끝까지 3회 기록으로 채웠다. 단계 마무리 점검(이 문서의 네 항목)은 하지 않아 2단계를 끝났다고 표시하지 않았다. 고치기 전의 사정: 완료 기준 중 2-1(옛 static 02가 실제 모델로 수정안 PR까지 감)만 남았었다. 2026-11-20의 닫기 작업으로 검사기가 `_seen_orders.append(order)`를 잡고(검사기 작성만 6/6, 끝까지 3/3. 모델이 ast-grep으로 가릴 수 없다고 답하면 Python AST로 바꾼다) 규칙 읽기가 그 규칙을 고칠 규칙(`model`)으로 읽는다(3/3). 남은 곳은 수정 판단 모델이 그 위반을 `skip`하는 것이다(끝까지 3/3, "고치는 방법이 여럿이라 사람이 골라야 한다"). 4.6이 모델에 맡긴 판단이라 파이프라인 쪽에서는 멈췄다. 남은 선택지는 저장소 주인이 정한다(BACKLOG의 2-1 항목, `docs/JOURNAL.md`).
- 문서와 코드가 다른 곳은 `docs/DESIGN.md` 12.2절에 있다. 코드 주석의 "DESIGN.md F7" 같은 번호는 v1의 절이고, 12.3절의 대응표로 읽는다.
- 작업은 기능 단위 브랜치와 PR로 진행한다.
- 첫 대상 리포는 별도 예제 리포 `Eunryong/eval`이다. 이 리포(`pr-proof`)는 자기 PR을 검사하지 않는다.
- 실제 저장소 `Eunryong/MathLLM`도 허용 목록에 있다. 2026-11-19에 pr-proof#21을 머지한 뒤로 v2 1단계 코드가 두 저장소를 같이 검사한다. MathLLM은 저장소 주인이 실제로 쓰는 코드다.
- 2026-11-19에 팀의 공개 저장소 `Pig30nidaE/BooBug`를 붙이기로 했고, 그 전에 임시 가드레일(작성자가 OWNER·MEMBER·COLLABORATOR가 아니면 아무것도 하지 않음)과 모델 전용 작업 폴더·경로 감시 훅을 넣었다(DESIGN 8.4, 8.6, 위협 표 T2·T28). 승인 기능은 6단계의 정식 가드레일로 2026-11-20에 넣었다(위). 실제 모델을 부르는 확인은 `PR_PROOF_REAL_AI=1 pytest tests/worker/ai/test_workdir_real_cli.py`다. 이 저장소에서 운영자의 권한은 쓰기이고, 웹훅은 저장소 주인만 등록할 수 있다.

## 실행

```
docker compose up -d       # redis
source .venv/bin/activate  # Procfile의 python·uvicorn은 venv에서 찾는다
cp .env.example .env       # 처음 한 번. API_PORT 등 로컬 실행 설정 (비밀값은 넣지 않음)
honcho start               # api(웹훅 + 동기화 루프), worker
pytest                # 파이프라인 자체 테스트 (Docker 없이)
pytest -m docker      # 실제 Docker·예제 리포가 필요한 통합 테스트 (이미지 빌드와 Python 런타임·ast-grep·ruff 받기에 네트워크가 필요하다)
pip install -e '.[rag]'   # 과거 기록 검색(rag.enabled)을 쓸 때만. chromadb·onnxruntime·tokenizers·numpy
python -m app.cli reindex # 벡터 저장소(data_dir/chroma/)를 SQLite의 기록으로 다시 만든다
python -m app.cli eval --baseline --db <경로>  # 평가 세트 전부를 돌려 eval_runs에 기준선으로 남긴다. --baseline 없이 돌리면 기준선과 비교 (--real-model은 claude를 부른다)
PR_PROOF_REAL_EMBED=1 PR_PROOF_EMBED_DIR=<모델 폴더> pytest tests/worker/test_real_embed.py  # 실제 임베딩 모델의 recall@k
```

워커 실행 전 `gh auth login`, `claude` 로그인, Docker 실행이 필요하다. preflight가 확인한다.

## 코드 구조

- 웹(api)은 GitHub 변화를 감지해 `PRJob`을 큐에 넣기만 한다. 감지 방법은 웹훅과 동기화(api 안의 주기 루프) 두 가지다
- 워커는 큐에서 작업을 꺼내 처리하고 결과(코멘트, 제안 PR)를 GitHub에 남긴다
- 둘 사이에는 큐와 Redis·SQLite 상태만 오간다. 키 형식과 테이블 정의는 `models/`, 읽기·쓰기 코드는 각자 자기 쪽에 둔다
- `app/models/`: 웹·워커 공용 데이터. 외부 호출 없음
- `app/controllers/`, `app/services/`, `app/views/`: 웹 쪽 MVC (webhook, sync). GitHub은 조회만 한다
- `app/worker/`: 워커 진입점, 작업 흐름(LangGraph), PR 코멘트, 처리 기록 쓰기, GitHub 쓰기
- `app/sandbox/`: 워커가 쓰는 언어와 무관한 docker 도구 (`docker_cli`, `container`, `toolcache`, `checker_runner`, `safe_read`)
- `app/worker/lang/python.py`: Python 언어 프로필 (런타임 준비, `.venv` 설치, pytest 실행과 결과 읽기, 위반 줄의 커버리지 읽기, ruff). 판단 규칙(테스트 파일의 정의, coverage.xml 파싱, ruff 설정 읽기, 도구 목록)은 `models/`에 있다. 두 번째 언어와 프로필 인터페이스는 만들지 않는다
- 규칙 경로: `worker/rules.py`(규칙 문서 읽기) · `worker/base_config.py`(base 설정, ruff 설정과 버전) → `worker/checkers.py`(검사 방법 정하기: 도구 설정의 규칙 묶음, 문서 규칙의 도구 선택, 검사기 작성) → `worker/detect.py`(위반 찾기) · `worker/judge.py`(모델의 직접 판단) → `worker/decide.py`(수정 판단) → `worker/mechanical_fix.py`(기계적 수정) · `worker/model_fix.py`(모델 수정, 모델 전용 폴더는 `worker/ai/workdir.py`) → `worker/proposal.py`(수정안 PR) · `worker/records.py`(기록) · `worker/memory.py`(검색: 임베딩, Chroma 저장과 top-k, 밀린 기록 임베딩, reindex, 큰 규칙 문서의 조각 검색). 판단 규칙은 같은 이름의 `models/` 모듈에 있다. 도구 목록과 ruff 설정·버전 읽기는 `models/tools.py`, 도구 선택은 `models/tool_choice.py`. 검색의 기록 텍스트·고르기·프롬프트 주입과 임베딩 모델 목록은 `models/rag.py`, 큰 규칙 문서의 조각은 `models/doc_sections.py`
- 도구(ast-grep, ruff)와 Python 런타임은 `sandbox/toolcache.py`가 고정한 버전과 SHA256으로 받아 캐시한다. 샌드박스 이미지에는 언어가 없다 (`debian:bookworm-slim`에 curl과 인증서)
- 웹 쪽은 `worker/`·`sandbox/`를, 워커는 웹 쪽을 import하지 않는다. `tests/test_layers.py`가 검사한다
- 테스트는 `tests/` 아래 같은 이름의 폴더에 둔다

## 코드 스타일

- Python 3.11+, 타입 힌트 필수, ruff로 포맷·린트
- 모듈 수준 상수(대문자 이름)는 `typing.Final`로 선언한다. dict 같은 가변 값은 `Final[Mapping[...]]`로 읽기 전용 타입을 준다. `tests/test_conventions.py`가 검사한다
- 테스트에서 바꿔야 하는 값은 상수를 monkeypatch하지 말고 함수 인자로 받는다
- 새 기능에는 단위 테스트를 같이 작성한다. 테스트는 경계값과 실패 경로를 포함한다.
