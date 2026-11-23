# 규칙 단계의 프롬프트

규칙을 읽고, 확인 방법을 정하고, 위반을 판단하는 단계의 지시문(시스템 프롬프트)이다.

| 단계 | 파일 | 읽는 모듈 (`app/worker/rules/`) |
| --- | --- | --- |
| 규칙 문서 읽기 | `rule_reading.md` | `rule_reading_pure.py` |
| 검사 도구 고르기 | `tool_choice.md` | `tool_choice_pure.py` |
| 검사기 작성 (ast-grep) | `checker_authoring.common.md` + `checker_authoring.ast_grep.md` | `checker_authoring_pure.py` |
| 검사기 작성 (Python ast) | `checker_authoring.common.md` + `checker_authoring.python_ast.md` | `checker_authoring_pure.py` |
| 검사기 작성 (경로 패턴) | `checker_authoring.path.md` | `checker_authoring_pure.py` |
| 위반 판단 | `judge.md` | `judge_pure.py` |
| PR 단위 확인: 계획 | `pr_check.plan.md` | `pr_check_pure.py` |
| PR 단위 확인: 판단 | `pr_check.judge.md` | `pr_check_pure.py` |
| PR 설명과 diff 견주기 | `pr_description.md` | `pr_description_pure.py` |
| 연결된 이슈 확인 | `linked_issues.md` | `linked_issues_pure.py` |
| 실행 결과의 규칙 | `run_check.md` | `run_check_pure.py` |

`checker_authoring.records.md`는 지난 기록을 자료로 줄 때만 검사기 작성 지시문 끝에 빈 줄 하나를
두고 붙는다.

## 고칠 때

- 파일의 글이 그대로 모델에게 간다. 이 파일(README.md)은 모델에게 가지 않는다.
- 글이 한 글자라도 바뀌면 그 단계의 프롬프트 판(글의 해시)이 바뀌어, 저장해 둔 모델의 답을 다시
  쓰지 않고 새로 묻는다.
- 파일 끝의 줄바꿈 하나는 글에 들지 않는다.
- 읽는 곳은 `app/worker/core/prompt_text_pure.py`의 `load(__file__, "<이름>")`이고, 부르는 모듈과
  같은 폴더의 `prompts/`만 읽는다. 이름은 영문 소문자와 밑줄, 점 하나까지다.
- 사용자 메시지(규칙 원문, diff 같은 자료를 담는 틀)는 각 모듈의 `build_prompt` 함수가 만든다.
  자료와 섞여 있어 코드에 둔다.
- 다른 단계의 지시문: `app/worker/rules/prompts/`, `app/worker/fix/prompts/`,
  `app/worker/feedback/prompts/`.
