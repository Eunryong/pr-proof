# 평가 케이스

예제 리포(`Eunryong/eval`)에 위반을 일부러 넣은 PR을 만들고, 파이프라인이 무엇을 찾고 어떻게 고쳐야 하는지를 정답으로 적은 것이다 (docs/DESIGN.md "회귀 테스트").

- `lint/*.yaml`: 2단계 컨벤션(lint) 케이스. `tests/e2e/test_lint_cases.py`가 읽어, 로컬 git 원격 + 실제 ruff + 실제 샌드박스 테스트로 처음부터 끝까지 돌린다. GitHub만 가짜로 바꾼다.
- `static/*.yaml`: 3단계 정적변수(static) 케이스. `tests/e2e/test_static_cases.py`가 같은 방식으로 돌린다. AI는 케이스의 `ai` 편집(회차마다 하나, 모자라면 마지막 것을 다시 씀)을 적용하는 가짜이고, `PR_PROOF_REAL_AI=1`이면 로그인된 claude를 실제로 부른다(비용이 든다). `expect.proposal`은 `auto | uncovered | tests`(auto: 기본값대로 검사 뒤 바로 수정), `expect.fix`는 `ready | ai_failed` 등. `ai_only_scripted: true`인 케이스는 실제 AI로 돌릴 때 건너뛴다.
- `rules/*.yaml`: v2 1단계 문서 규칙 케이스. 예제 리포 README의 "코드 규칙" 절의 규칙을 읽어 검사기를 만들고 PR이 바꾼 줄의 위반을 찾는지 본다. `tests/e2e/test_rules_cases.py`가 같은 방식으로 돌리고, base에 `.pipeline.yaml`(`skills.enabled: []`)을 넣어 v1 lint·static을 끈다. 모델은 케이스의 `model` 답을 돌려주는 가짜이고(원문 확인, ast-grep 예시 확인, 검사기 실행은 진짜), `PR_PROOF_REAL_MODEL=1`이면 로그인된 claude를 실제로 부른다(비용이 든다. 비교용). `scripted_only: true`인 케이스는 실제 모델로 돌릴 때 건너뛴다.
- 실행: `pytest -m docker tests/e2e`
- 6단계에서 케이스를 20–30개로 늘리고, 결과를 `eval_runs`에 남겨 기준선과 비교한다.

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
scripted_only: false     # (선택) 가짜 모델로만 돌린다
pipeline: {}             # (선택) base의 .pipeline.yaml에 더할 값 (기본은 skills.enabled: [])
base: [...]              # (선택) base 커밋의 변경. 형식은 위와 같고 create: 내용으로 새 파일을 만든다
pr: [...]
model:                   # 가짜 모델의 답 (실제 모델로 돌리면 쓰지 않는다)
  rules:                 # 규칙 읽기의 답. quote는 base README의 그 절에 있어야 채택된다
    - section: 코드 규칙 > 출력
      quote: "`orders/` 아래 모듈은 `print`를 쓰지 않는다."
      scope: ["orders/**"]          # 문서가 밝힌 범위. 비면 저장소 전체
      scope_exclude: []             # (선택) 범위가 없는 규칙에서 모델이 뺀 경로
      scope_reason: ""
      checker:                      # 이 규칙의 검사기 작성 답 (ast-grep 규칙과 예시)
        body: |
          ...
        examples: [{code: "print(1)\n", violates: true, lines: [1]}, ...]
expect:
  violations: [파일:줄, ...]   # 문서 규칙 절의 위반 목록과 정확히 같아야 한다
  not_flagged: [파일:줄, ...]  # 잡히면 안 되는 바뀐 줄
  contains: [...]              # 문서 규칙 절에 있어야 할 글
  absent: [...]                # 없어야 할 글
  second_pr_without_calls: true  # 같은 base의 두 번째 PR은 모델 호출 0회
```

모델 수정까지 보는 케이스(`09`–`12`, 옛 static 케이스를 옮긴 것)는 아래를 더한다. `fix`가 없는 케이스는
수정 판단이 모든 위반을 skip으로 답해 수정까지 가지 않는다.

```yaml
model:
  skip:                    # 수정 판단이 skip으로 답할 위반과 이유. 나머지는 fix
    orders/services/pricing.py:21: 이유
fix:                       # 가짜 수정 모델 (실제 모델로 돌리면 쓰지 않는다)
  edits:                   # 회차마다 모델 폴더에 적용할 편집 (모자라면 마지막 것을 다시 쓴다)
    - - {file: 경로, find: 바꿀 문자열, replace: 바꿀 내용}
  answer:                  # 마지막 답(설명 JSON). rule은 model.rules의 순번
    summary: 요약
    changes: [{file: 경로, line: 13, rule: 0, why: 이유}]
expect:
  fix: ready               # ready | failed | budget | none (budget: 호출 상한, none: 부르지 않음)
  fix_file: 경로           # ready일 때 수정안 브랜치에서 바뀐 유일한 파일
  same_as_base: true       # 그 파일이 base와 같아야 함
  pushed_contains: [...]   # 그 파일에 있어야 할 글
  pr_contains: [...]       # 수정안 PR 본문에 있어야 할 글
  pr_absent: [...]
  fix_command:             # (선택) 결과 코멘트 뒤에 명령을 달고 다시 본다 (14: 2-11)
    command: /fix
    fix: ready             # 그 뒤의 수정 결과. fix_file 등 위와 같은 키를 쓴다
    contains: [...]        # 그 뒤 문서 규칙 절에 있어야 할 글
    absent: [...]
```
