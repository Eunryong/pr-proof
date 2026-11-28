# 환경 처방의 프롬프트

저장소의 base 커밋을 읽고 그 저장소를 빌드·테스트할 환경 처방을 쓰는 단계의 지시문(시스템
프롬프트)이다.

| 단계 | 파일 | 읽는 모듈 (`app/worker/env/`) |
| --- | --- | --- |
| 환경 처방 정하기 | `plan_env.md` | `plan_pure.py` |

`plan_env.md`의 `$env_root`, `$src_dir`, `$overlay_dir`, `$out_dir` 자리에는
`app/worker/env/recipe_pure.py`의 같은 이름의 상수가 들어간다.

## 고칠 때

- 파일의 글이 그대로 모델에게 간다. 이 파일(README.md)은 모델에게 가지 않는다.
- 글이 한 글자라도 바뀌면 프롬프트 판(글의 해시)이 바뀌어, 저장해 둔 처방을 다시 쓰지 않고 새로
  정한다.
- 파일 끝의 줄바꿈 하나는 글에 들지 않는다.
- 읽는 곳은 `app/worker/core/prompt_text_pure.py`의 `load(__file__, "<이름>")`이고, 부르는 모듈과
  같은 폴더의 `prompts/`만 읽는다.
- 사용자 메시지(회차, 플랫폼, 루트 목록, 직전 답과 확인 결과를 담는 틀)는 `plan_pure.py`의
  `build_prompt`가 만든다. 자료와 섞여 있어 코드에 둔다.
