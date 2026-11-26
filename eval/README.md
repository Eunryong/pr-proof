# 평가 케이스

예제 리포(`Eunryong/eval`)에 위반을 일부러 넣은 PR을 만들고, 파이프라인이 무엇을 찾고 어떻게 고쳐야 하는지를 정답으로 적은 것이다 (docs/DESIGN.md "회귀 테스트").

- `lint/*.yaml`: v1 2단계 컨벤션(lint) 케이스. v2 3단계부터는 도구 설정의 규칙 묶음과 기계적 수정 묶음으로 돈다(완료 기준 3-1). `tests/e2e/test_lint_cases.py`가 읽어, 로컬 git 원격 + 실제 ruff(도구 캐시에 받음) + 실제 샌드박스 테스트로 처음부터 끝까지 돌린다. GitHub만 가짜로 바꾼다. 예제 리포에는 ruff 설정이 없어 하네스가 base에 빈 `ruff.toml`을 둔다. 케이스 파일은 v1 그대로이고 수정안 브랜치만 `fix-mechanical`이다.
- `tools/*.yaml`: v2 3단계 도구 연결 케이스(3-2–3-5). 문서 규칙을 ruff 코드로 잇기, ruff를 받지 않는 base, 규칙이 없는 base, 저장소가 고정한 ruff 버전과 두 번째 실행. `tests/e2e/test_tool_cases.py`가 케이스마다 빈 도구 캐시로 돌려 무엇을 받았는지 본다. 형식은 그 파일의 독스트링에 있다.
- 옛 `static/*.yaml` 다섯 개(파이프라인 내장 정적변수 검사)는 v2 2단계에 내장 검사기와 함께 지웠다. 확인하던 것은 README 규칙 케이스 `rules/09`–`12`, `15`, `16`으로 옮겼다(01 매직 값의 수정, 02 모듈 수준 상태의 수정, 03 테스트가 지나가지 않는 위반(2026-11-21부터 고치고, 가짜 수정이 테스트를 만들지 않아 "있던 테스트도 만든 테스트도 이 줄을 실행하지 않음"으로 적는다), 04 고치면 테스트가 깨지는 수정, 05 테스트가 실패한 PR, 그리고 README에 그 문장이 없는 base).
- `rules/*.yaml`: v2 1단계 문서 규칙 케이스. 예제 리포 README의 "코드 규칙" 절의 규칙을 읽어 검사기를 만들고 PR이 바꾼 줄의 위반을 찾는지 본다. `tests/e2e/test_rules_cases.py`가 같은 방식으로 돌린다. 예제 리포에는 ruff 설정이 없어 도구 설정의 규칙은 없고, 가짜 모델은 도구 선택에 규칙의 `tool`(없으면 맞는 도구 없음)을 답한다. 모델은 케이스의 `model` 답을 돌려주는 가짜이고(원문 확인, ast-grep 예시 확인, 검사기 실행은 진짜), `PR_PROOF_REAL_MODEL=1`이면 로그인된 claude를 실제로 부른다(비용이 든다. 비교용). `scripted_only: true`인 케이스는 실제 모델로 돌릴 때 건너뛴다. `real_only: true`인 케이스는 가짜 답(`model`)이 없고 실제 모델로만 돈다(2026-11-22 저장소 주인: 가짜 모델을 없애고 실제 모델로 확인한다). 기대는 글이 아니라 결과(위반 위치, 수정안이 올라갔는가, 만든 테스트가 올라갔는가, 실행 상태)로 적는다. 새 케이스는 이 모양으로 만든다.
- `judge/*.yaml`: v2 4단계 모델의 직접 판단 케이스(4-1–4-7). 검사기 작성이 세 번 모두 예시를 통과하지 못한 규칙이 모델 판단으로 내려가, 위반이 수정안 PR까지 가는지, 답의 줄 인용 확인, hunk의 지시 주석(기록만), 위반 수에 비례하는 PR 상한과 `/fix`, 저장소 하루 상한을 본다. `tests/e2e/test_judge_cases.py`가 문서 규칙 케이스와 같은 방식으로 돌리고, 더한 키(`model.rules[].judge`, `record_only`, `real_checker_scripted`, `expect.judge_calls`·`next_commit`·`then_fix`·`mechanical_branch`)는 그 파일의 독스트링에 있다.
- `tools_off.yaml`: 도구를 끈 평가(DESIGN 13절, 완료 기준 1-16, 3-7, 3-8, 4-8)의 표본과 개념. 위 케이스 열 개(옛 static 다섯과 lint 다섯)와 1단계 기준선의 probe 파일(ruff가 정답지), 판단 케이스 `judge/01`·`06`(케이스가 표시한 줄이 정답)을 같은 규칙 읽기 결과로 모델 판단만·도구 끔·도구 켬에서 돌려, 개념과 확인 방법(도구 / 검사기 / 모델 판단)마다 재현율·정밀도를 낸다. `tests/e2e/test_tools_off_eval.py`가 읽고, 실행 방법은 그 파일의 독스트링에 있다.
- 실행: `python -m app.worker.cli eval --baseline`으로 `tests/e2e`의 docker 테스트 전부(케이스 파일, 도구를 끈 평가, 비교 하네스)를 돌려 결과를 SQLite의 `eval_runs`에 기준선으로 남긴다. 프롬프트나 모델을 바꾼 뒤 `python -m app.worker.cli eval`을 돌리면 같은 종류(가짜 모델 / `--real-model`)의 가장 최근 기준선과 비교한다(6단계, 완료 기준 6-1, 6-3). 하네스만 보려면 `pytest -m docker tests/e2e`.
- 케이스 수 (2026-11-21): `rules` 31, `judge` 6, `lint` 5, `tools` 4, `rag` 10, 합 56. 2026-11-22에 `rules/32`를 하나 더했다(폴더의 파일 수로 세면 `rules`에 번호 31이 둘이라 파일은 33개다): 있던 테스트가 닿지 않는 위반 줄을 수정 작성의 모델이 테스트를 만들어 확인하며 고치고, 출구 검사가 그 테스트를 받아 수정안 PR에 같이 올린다(실제 모델로만, `real_only`). 받아들여지지 않는 테스트가 빠지는 길은 실제 모델로 일부러 만들 수 없어 단위·흐름 테스트가 본다. `rules/31`은 2026-11-21에 더했다: 다른 파일이 import하는 이름이라 고칠 수 없는 위반이 이유와 함께 사람에게 남는다. 팀 고유 규칙 케이스(DESIGN 13절 "eval에 더할 케이스: 팀 고유 규칙 (`eval/rules/`)")는 2026-11-20까지 `rules`의 30개였다(린터에도 있는 옛 static 여덟 개를 빼고 판단 케이스를 더해 세면 28개). `rules/17`–`30`은 6단계에 더했다: 금액 타입(17), 모델 메서드의 `self` 대입(18), 예외 이름(19), 상수 위치(20), 환경변수 위치(21), 테스트의 `time.sleep`(22), 로거 이름(23), `requests` import 위치(24), 현재 시각 읽기(25), 금액 반올림의 모델 수정(26), API 함수 이름(27), services의 `dict` 인자(28), models의 `logging` import(29), 짝수 쪽 반올림의 모델 수정(30). 26과 30은 6-2 모델 비교의 표본이다.
- `tests/e2e/test_model_compare.py`: 수정 작성의 모델 세 구성(가벼운 모델만 / 무거운 모델만 / 가벼운 다음 무거운)을 `rules/09`, `10`, `26`, `30`으로 비교한다(6-2). 해소율·채택률·호출 수를 낸다. 실행 방법은 그 파일의 독스트링에 있다.

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
real_only: false         # (선택) 실제 모델로만 돌린다. 이때 model은 없어도 된다
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
  body_contains: [...]         # 결과 코멘트 전체("설정:" 포함)에 있어야 할 글
  second_pr_without_calls: true  # 같은 base의 두 번째 PR은 모델 호출 0회
```

모델 수정까지 보는 케이스(`09`–`12`, 옛 static 케이스를 옮긴 것)는 아래를 더한다. `fix`가 없는 케이스는
수정 판단이 모든 위반을 skip으로 답해 수정까지 가지 않는다.

```yaml
model:
  skip:                    # 수정 판단이 skip으로 답할 위반과 이유. 나머지는 fix
    orders/services/pricing.py:21: 이유            # 이유 글만 (skip 이유 이름 없음)
    orders/services/pricing.py:30: {reason: changes_behavior, why: 이유}
fix:                       # 가짜 수정 모델 (실제 모델로 돌리면 쓰지 않는다)
  edits:                   # 회차마다 모델 폴더에 적용할 편집 (모자라면 마지막 것을 다시 쓴다)
    - - {file: 경로, find: 바꿀 문자열, replace: 바꿀 내용}
  answer:                  # 마지막 답(설명 JSON). rule은 model.rules의 순번
    summary: 요약
    changes: [{file: 경로, rule: 0, why: 이유}]  # (파일, 규칙)마다 하나. 줄은 파이프라인이 diff에서 채운다. line을 넣어도 읽지 않는다
expect:
  fix: ready               # ready | failed | budget | none (budget: 호출 상한, none: 부르지 않음)
  fix_file: 경로           # ready일 때 수정안 브랜치에서 바뀐, 위반이 있는 파일
  fix_also: [경로, ...]    # (선택) 위반을 고치며 같이 바뀐, 위반이 없는 파일
  generated_tests: true    # (선택) fix_file과 함께 만든 테스트(tests/test_pipeline_*.py)가 올라가야 한다
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
