# 수정 단계의 프롬프트

위반을 고칠지 정하고 수정을 쓰는 단계의 지시문(시스템 프롬프트)이다.

| 단계 | 파일 | 읽는 모듈 (`app/worker/fix/`) |
| --- | --- | --- |
| 수정할지 정하기 | `fix_decision.md` | `fix_decision_pure.py` |
| 수정 작성 | `model_fix.md` | `model_fix_pure.py` |

조건이 맞을 때만 위 지시문 끝에 빈 줄 하나를 두고 붙는 조각이다.

| 조각 | 붙는 곳 | 붙는 때 | 읽는 모듈 |
| --- | --- | --- | --- |
| `fix_decision.records.md` | 수정할지 정하기 | 지난 기록을 자료로 줄 때 | `fix_decision_pure.py` |
| `model_fix.records.md` | 수정 작성 | 지난 기록을 자료로 줄 때 | `model_fix_pure.py` |
| `model_fix.feedback.md` | 수정 작성 | PR 코멘트를 받아 고칠 때 | `feedback_fix_pure.py` |

`model_fix.md`의 `$check_rules`, `$run_tests`, `$max_test_files`, `$max_test_lines`는 코드가 값을
넣는 자리다(도구 이름과 상한). 수정 작성에서 모델이 부르는 도구의 설명은
`app/worker/model/mcp_pure.py`에 있다.

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
