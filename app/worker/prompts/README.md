# 프롬프트

모델에게 주는 지시문(시스템 프롬프트)이 모두 이 폴더에 있다. 파일의 글이 그대로 모델에게 간다.
이 파일(README.md)은 모델에게 가지 않는다.

## 단계와 파일

| 단계 | 파일 | 읽는 모듈 |
| --- | --- | --- |
| 규칙 문서 읽기 | `rule_reading.md` | `app/worker/rules/rule_reading_pure.py` |
| 검사 도구 고르기 | `tool_choice.md` | `app/worker/rules/tool_choice_pure.py` |
| 검사기 작성 (ast-grep) | `checker_authoring.common.md` + `checker_authoring.ast_grep.md` | `app/worker/rules/checker_authoring_pure.py` |
| 검사기 작성 (Python ast) | `checker_authoring.common.md` + `checker_authoring.python_ast.md` | 위와 같음 |
| 검사기 작성 (경로 패턴) | `checker_authoring.path.md` | 위와 같음 |
| 위반 판단 | `judge.md` | `app/worker/rules/judge_pure.py` |
| PR 단위 확인: 계획 | `pr_check.plan.md` | `app/worker/rules/pr_check_pure.py` |
| PR 단위 확인: 판단 | `pr_check.judge.md` | 위와 같음 |
| PR 설명과 diff 견주기 | `pr_description.md` | `app/worker/rules/pr_description_pure.py` |
| 연결된 이슈 확인 | `linked_issues.md` | `app/worker/rules/linked_issues_pure.py` |
| 실행 결과의 규칙 | `run_check.md` | `app/worker/rules/run_check_pure.py` |
| 수정할지 정하기 | `fix_decision.md` | `app/worker/fix/fix_decision_pure.py` |
| 수정 작성 | `model_fix.md` | `app/worker/fix/model_fix_pure.py` |
| PR 코멘트 가리기 | `feedback_triage.md` | `app/worker/feedback/feedback_triage_pure.py` |

## 뒤에 덧붙는 조각

조건이 맞을 때만 위 지시문 끝에 빈 줄 하나를 두고 붙는다.

| 조각 | 붙는 곳 | 붙는 때 |
| --- | --- | --- |
| `checker_authoring.records.md` | 검사기 작성 | 지난 기록을 자료로 줄 때 |
| `fix_decision.records.md` | 수정할지 정하기 | 지난 기록을 자료로 줄 때 |
| `model_fix.records.md` | 수정 작성 | 지난 기록을 자료로 줄 때 |
| `model_fix.feedback.md` | 수정 작성 | PR 코멘트를 받아 고칠 때 |

## 고칠 때

- 파일의 글이 한 글자라도 바뀌면 그 단계의 프롬프트 판(글의 해시)이 바뀌어, 저장해 둔 모델의 답을
  다시 쓰지 않고 새로 묻는다. 규칙 읽기를 고치면 붙은 저장소가 규칙 문서를 한 번씩 다시 읽는다.
- 파일 끝의 줄바꿈 하나는 글에 들지 않는다.
- `model_fix.md`의 `$check_rules`, `$run_tests`, `$max_test_files`, `$max_test_lines`는 코드가 값을
  넣는 자리다(도구 이름과 상한). 다른 파일에서 `$`는 그대로 글이다.
- 새 파일을 더하면 `app/worker/prompt_text_pure.py`의 `load("<이름>")`으로 읽는다. 이름은 영문
  소문자와 밑줄, 점 하나까지다.

## 이 폴더에 없는 것

- 사용자 메시지(규칙 원문, diff, 위반 목록 같은 자료를 담는 틀)는 각 단계 모듈의 `build_prompt`
  함수가 만든다. 자료와 섞여 있어 코드에 둔다.
- 수정 작성에서 모델이 부르는 도구의 설명은 `app/worker/model/mcp_pure.py`에 있다.
