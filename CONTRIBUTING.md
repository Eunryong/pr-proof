# 기여 안내

이 저장소의 코드 규칙이다. pr-proof가 이 문서의 규칙을 PR의 diff에서 검사한다. 한 문장에 규칙 하나를 적는다.

## 코드 규칙

### 상수

`app/`과 `tests/` 아래 Python 모듈의 최상위에서 대문자 이름(`MAX_ROWS`, `_TIMEOUT`처럼 대문자·숫자·`_`로 된 이름. 앞에 `_` 하나가 붙어도 같다)에 값을 대입하면 `typing.Final`로 선언한다.

```python
# 어김
MAX_ROWS = 100
_TIMEOUT: int = 30

# 지킴
MAX_ROWS: Final = 100
_TIMEOUT: Final[int] = 30
LIMITS: typing.Final[Mapping[str, int]] = {"pr": 14}
```

함수와 클래스 안의 이름, 대문자로만 되지 않은 이름(`Value`), 여러 이름에 한꺼번에 대입하는 문장(`A, B = 1, 2`)은 이 규칙의 대상이 아니다.

### 패키지

저장소 루트에서 `__init__.py`가 있는 폴더는 `app`과 `tests`뿐이다.

`app` 바로 아래 패키지는 `api`(웹), `worker`(워커), `shared`(웹과 워커가 같이 쓰는 것), `setup`(사람이 치는 설치·실행 명령) 넷뿐이다.

`pyproject.toml`의 `[tool.setuptools.packages.find]`에서 `include`는 `["app*"]`이다.

### 계층

아래 import 규칙은 파일 어디에 있는 import든(함수 안 포함) `import x`와 `from x import y`를 모두 센다. 상대 import(`from . import x`)는 그 모듈의 전체 이름(`app.worker.fix.x`)으로 본다.

`app/api/` 아래 모듈은 `app` 안에서 `app.api`와 `app.shared`만 import한다. api 프로세스는 PR 코드를 실행하지 않으므로 `app.worker`를 import하지 않는다.

`app/worker/` 아래 모듈은 `app` 안에서 `app.worker`와 `app.shared`만 import한다.

`app/shared/` 아래 모듈은 `app` 안에서 `app.shared`만 import한다.

`app/setup/` 아래 모듈은 `app` 안에서 `app.setup`, `app.shared`, `app.worker`만 import한다. 서버의 시작 확인(`app.worker.preflight`)을 그대로 다시 쓰기 때문이고, 거꾸로 `app.api`·`app.worker`·`app.shared`는 `app.setup`을 import하지 않는다.

`app/worker/`는 세 층이다(pr-proof#98). 맨 위에는 뼈대만 있고, 나머지는 단계 폴더와 공용 폴더에 있다.

- 뼈대: `app/worker/` 바로 아래의 `main`, `graph`, `flow_state`, `flow_nodes`, `update`, `command`, `command_pure`, `feedback_job`, `guard`, `retry`, `deps`, `cli`, `preflight`, `records`. 단계를 부르기만 한다.
- 단계 폴더: `rules`, `feedback`, `fix`, `report`, `evaluation`. 한 단계가 하는 일 전부를 둔다.
- 공용 폴더: `core`, `sandbox`, `testing`, `lang`, `env`, `git`, `config`, `store`, `model`. 모든 단계가 쓰는 데이터 정의와 도구를 둔다.

단계 폴더 아래 모듈은 뼈대 모듈을 import하지 않는다.

공용 폴더 아래 모듈은 단계 폴더의 모듈과 뼈대 모듈을 import하지 않는다.

단계 폴더 사이에는 `fix`가 `rules`·`feedback`을, `report`와 `evaluation`이 `rules`·`feedback`·`fix`를 import하는 방향만 쓴다.

공용 폴더 사이에는 다음 방향만 쓴다: `sandbox`는 `core`를, `testing`은 `core`·`sandbox`를, `lang`은 `core`·`sandbox`·`testing`을, `env`는 `core`·`sandbox`·`testing`·`git`·`config`·`store`·`model`을, `git`은 `core`·`sandbox`를, `config`는 `core`·`sandbox`·`git`을, `store`는 `core`·`sandbox`·`git`·`config`를, `model`은 `core`·`sandbox`·`git`·`config`·`store`를 import하고, `core`는 다른 폴더를 import하지 않는다.

`app/worker/sandbox/` 아래 모듈은 `app` 안에서 `app.worker.sandbox`의 모듈과 순수 모듈만 import한다.

```python
# app/worker/sandbox/container.py
# 어김
from app.worker import repo

# 지킴
from app.worker.sandbox import docker_cli
from app.worker.core.checker_pure import CheckerKind
```

### 순수 모듈

순수 모듈은 `app/` 아래에서 파일 이름이 `_pure.py`로 끝나는 모듈이다.

순수 모듈은 `app` 안에서 이름이 `_pure`로 끝나는 다른 순수 모듈만 import한다.

순수 모듈은 그것이 속한 단계 폴더나 공용 폴더에 두고, 어느 한 폴더에 속하지 않으면서 여러 단계가 쓰는 데이터 정의와 도구만 `app/worker/core/`에 둔다.

순수 모듈은 `os`, `shutil`, `tempfile`, `subprocess`, `socket`, `http`, `urllib`, `sqlite3`, `redis`, `rq`, `httpx`, `requests`, `fastapi`, `uvicorn`, `langgraph`, `chromadb`, `onnxruntime`, `tokenizers`, `logging`, `time`, `random`, `importlib`을 import하지 않는다(파일 시스템, 프로세스, 네트워크, Redis, 큐, DB, 웹, 그래프, 벡터 저장소, 로그, 시계, 난수, 동적 import).

순수 모듈은 내장 함수 `open(...)`을 부르지 않는다.

순수 모듈은 파일을 읽지 않는다. 모델에게 주는 지시문의 글(각 단계 폴더의 `prompts/` 아래의 파일)만 예외이고, `app/worker/core/prompt_text_pure.py`의 `load`로 같은 폴더의 `prompts/`만 읽는다.

```python
# app/worker/fix/decide_pure.py
# 어김
import subprocess
from app.worker import state
from app.shared.config import load_config

def read(path):
    return open(path).read()

# 지킴
from app.worker.fix.fix_decision_pure import FixDecision
```

순수 모듈에서 다른 함수를 부르는 것, `path.open()`처럼 속성으로 부르는 것은 이 규칙들의 대상이 아니다.
