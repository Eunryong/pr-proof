"""Docker 테스트가 쓰는 이미지 태그와 컨테이너 이름 접두어.

운영(honcho)과 같은 Docker를 쓰므로 이름을 나눈다. 같으면 테스트가 운영 이미지를 개발 중인
Dockerfile로 다시 빌드하고, runs ID가 같은 운영 컨테이너를 지울 수 있다. 접두어에 프로세스 번호를
붙여 두 세션이 같은 Docker에서 docker 테스트를 같이 돌려도 컨테이너 이름(빈 DB의 첫 작업은 늘
runs ID 1)이 겹치지 않게 한다.
"""

import os
from typing import Final

from app.worker.sandbox.docker_cli import Names

# 이미지 이름은 3단계에 최소 이미지로 바꾸며 새로 지었다. 예전 태그(Python 이미지)를 빌드하는 다른
# 작업 폴더의 테스트와 같은 태그를 번갈아 덮어쓰지 않는다
TEST_NAMES: Final = Names(
    image="pipeline-sandbox-minimal-test", container_prefix=f"pr-proof-test-{os.getpid()}"
)
