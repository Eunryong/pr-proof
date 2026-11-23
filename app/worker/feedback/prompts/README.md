# 리뷰 코멘트 단계의 프롬프트

PR에 달린 코멘트(Copilot, 팀원)를 받아들일지 가리는 단계의 지시문(시스템 프롬프트)이다.

| 단계 | 파일 | 읽는 모듈 (`app/worker/feedback/`) |
| --- | --- | --- |
| PR 코멘트 가리기 | `feedback_triage.md` | `feedback_triage_pure.py` |

받아들인 코멘트를 고칠 때 수정 작성 지시문에 붙는 조각은 `app/worker/fix/prompts/model_fix.feedback.md`다.

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
