# 평가 케이스

예제 리포(`Eunryong/eval`)에 위반을 일부러 넣은 PR을 만들고, 파이프라인이 무엇을 찾고 어떻게 고쳐야 하는지를 정답으로 적은 것이다 (docs/DESIGN.md "회귀 테스트").

- `lint/*.yaml`: 2단계 컨벤션(lint) 케이스. `tests/e2e/test_lint_cases.py`가 읽어, 로컬 git 원격 + 실제 ruff + 실제 샌드박스 테스트로 처음부터 끝까지 돌린다. GitHub만 가짜로 바꾼다.
- `static/*.yaml`: 3단계 정적변수(static) 케이스. `tests/e2e/test_static_cases.py`가 같은 방식으로 돌린다. AI는 케이스의 `ai` 편집(회차마다 하나, 모자라면 마지막 것을 다시 씀)을 적용하는 가짜이고, `PR_PROOF_REAL_AI=1`이면 로그인된 claude를 실제로 부른다(비용이 든다). `expect.proposal`은 `approval | uncovered | tests`, `expect.fix`는 `ready | ai_failed` 등. `ai_only_scripted: true`인 케이스는 실제 AI로 돌릴 때 건너뛴다.
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
  proposal: approval         # approval | no_fixable | tests
  fix: ready                 # (approval일 때) /fix lint 결과 상태
  remaining: []              # 고친 뒤 남는 규칙 코드
  file: orders/services/pricing.py   # 고친 뒤 확인할 파일
  contains: [...]            # 제안 브랜치의 그 파일에 있어야 할 문자열
  not_contains: [...]
  same_as_base: true         # 제안 브랜치의 그 파일이 base와 같아야 함
```
