# 평가 케이스

예제 리포(`Eunryong/eval`)에 위반을 일부러 넣은 PR을 만들고, 파이프라인이 무엇을 찾고 어떻게 고쳐야 하는지를 정답으로 적은 것이다 (docs/DESIGN.md "회귀 테스트").

- 모델이 끼는 케이스는 설정된 모델을 실제로 부른다. 모델의 답을 케이스에 적지 않는다. 기대는 글이 아니라 결과(위반 위치, 수정안이 올라갔는가, 바뀐 파일, 만든 테스트가 올라갔는가)로 적는다. `PR_PROOF_REAL_MODEL=1`이 없으면 이 케이스들은 건너뛴다.
- `lint/*.yaml`: 도구 설정의 규칙 묶음과 기계적 수정 묶음(완료 기준 3-1). `tests/e2e/test_lint_cases.py`가 읽어, 로컬 git 원격 + 실제 ruff(도구 캐시에 받음) + 실제 샌드박스 테스트로 처음부터 끝까지 돌린다. GitHub만 가짜로 바꾼다. 하네스가 base에 빈 `ruff.toml`을 두고 규칙 문서(README.md)를 지워, 모델을 부르지 않는다.
- `rules/*.yaml`: 문서 규칙 케이스. 예제 리포 README의 "코드 규칙" 절의 규칙을 읽어 PR이 바꾼 줄의 위반을 찾고 고치는지 본다. `tests/e2e/test_rules_cases.py`가 돌린다. `17`–`30`은 팀 고유 규칙이고 `26`, `30`, `31-money-signed`는 모델 비교의 표본이다.
- `judge/*.yaml`: 검사기로 가릴 수 없는 규칙의 모델 판단 케이스(4-1, 4-2, 4-5). `tests/e2e/test_judge_cases.py`가 돌리고, 더한 키(`record_only`, `expect.judge_calls`·`next_commit`·`then_fix`·`mechanical_branch`)는 그 파일의 독스트링에 있다.
- `pr/*.yaml`: PR 단위 확인 케이스(커밋 형식, PR 본문). `tests/e2e/test_pr_cases.py`.
- `tools/*.yaml`: 도구 연결 케이스(3-2–3-5). 문서 규칙을 ruff 코드로 잇기, ruff를 받지 않는 base, 규칙이 없는 base, 저장소가 고정한 ruff 버전과 두 번째 실행. `tests/e2e/test_tool_cases.py`가 케이스마다 빈 도구 캐시로 돌려 무엇을 받았는지 본다.
- `rag/*.yaml`: 과거 기록 검색 케이스. 임베딩 모델도 실제 모델이다(`PR_PROOF_REAL_EMBED=1`, `PR_PROOF_EMBED_DIR`). `tests/e2e/test_rag_cases.py`.
- `feedback/*.yaml`: PR 코멘트 케이스. `tests/e2e/test_feedback_cases.py`.
- `tools_off.yaml`: 도구를 끈 평가(DESIGN 13절, 완료 기준 1-16, 3-7, 3-8, 4-8)의 표본과 개념. lint 케이스와 문서 규칙 케이스, probe 파일(ruff가 정답지), 판단 케이스 `judge/01`·`06`(케이스가 표시한 줄이 정답)을 같은 규칙 읽기 결과로 모델 판단만·도구 끔·도구 켬에서 돌려, 개념과 확인 방법(도구 / 검사기 / 모델 판단)마다 재현율·정밀도를 낸다. `tests/e2e/test_tools_off_eval.py`.
- `tests/e2e/test_model_compare.py`: 수정 작성의 모델 세 구성(가벼운 모델만 / 무거운 모델만 / 가벼운 다음 무거운)을 비교한다(6-2). 해소율·채택률·호출 수를 낸다.
- 실행: `python -m app.worker.cli eval --baseline`으로 `tests/e2e`의 docker 테스트 전부를 돌려 결과를 SQLite의 `eval_runs`에 기준선으로 남긴다. 프롬프트나 모델을 바꾼 뒤 `python -m app.worker.cli eval`을 돌리면 가장 최근 기준선과 비교한다(완료 기준 6-1, 6-3). 모델을 부르므로 비용이 든다.

## 케이스 형식

```yaml
name: 케이스 이름
description: 무엇을 확인하는지
base:            # (선택) base 커밋에 미리 넣을 변경
  - file: 경로
    find: 바꿀 문자열      # 처음 나오는 곳 하나를 replace로 바꾼다 (없으면 케이스 오류)
    replace: 바꿀 내용
  - file: 경로
    append: 파일 끝에 덧붙일 내용     # 또는 prepend: 파일 앞에 붙일 내용
pr:              # PR 커밋의 변경 (형식은 base와 같다)
  - ...
expect:
  findings: [F401]           # PR이 바꾼 줄에서 찾아야 할 규칙 코드 (정렬)
  proposal: auto             # auto | no_fixable | tests (auto: 검사 뒤 바로 수정, 기본값)
  fix: ready                 # (auto일 때) 자동 수정 결과 상태
  remaining: []              # 고친 뒤 남는 규칙 코드
  file: orders/services/pricing.py   # 고친 뒤 확인할 파일
  contains: [...]            # 제안 브랜치의 그 파일에 있어야 할 문자열
  not_contains: [...]
  same_as_base: true         # 제안 브랜치의 그 파일이 base와 같아야 함
```

## 문서 규칙 케이스 형식 (`rules/`)

```yaml
name: 케이스 이름
description: 무엇을 확인하는지
pipeline: {}             # (선택) base의 .pipeline.yaml에 더할 값
base: [...]              # (선택) base 커밋의 변경. 형식은 위와 같고 create: 내용으로 새 파일을 만든다
pr: [...]
expect:
  violations: [파일:줄, ...]   # 문서 규칙 절의 위반 목록과 정확히 같아야 한다
  not_flagged: [파일:줄, ...]  # 잡히면 안 되는 바뀐 줄
  contains: [...]              # 문서 규칙 절에 있어야 할 글
  absent: [...]                # 없어야 할 글
  body_contains: [...]         # 결과 코멘트 전체("설정:" 포함)에 있어야 할 글
  second_pr_without_calls: true  # 같은 base의 두 번째 PR은 모델 호출 0회
  fix: ready               # (선택) ready | failed | budget | none (budget: 호출 상한, none: 부르지 않음)
  fix_file: 경로           # ready일 때 수정안 브랜치에서 바뀐, 위반이 있는 파일
  fix_also: [경로, ...]    # (선택) 위반을 고치며 같이 바뀐, 위반이 없는 파일
  generated_tests: true    # (선택) fix_file과 함께 만든 테스트(tests/test_pipeline_*.py)가 올라가야 한다
  pushed_contains: [...]   # 그 파일에 있어야 할 글
  pr_contains: [...]       # 수정안 PR 본문에 있어야 할 글
  pr_absent: [...]
  fix_command:             # (선택) 결과 코멘트 뒤에 명령을 달고 다시 본다
    command: /fix
    fix: ready             # 그 뒤의 수정 결과. fix_file 등 위와 같은 키를 쓴다
    contains: [...]        # 그 뒤 문서 규칙 절에 있어야 할 글
    absent: [...]
```
