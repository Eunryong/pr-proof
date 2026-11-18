"""Docker 테스트가 쓰는 이미지 태그와 컨테이너 이름 접두어.

운영(honcho)과 같은 Docker를 쓰므로 이름을 나눈다. 같으면 테스트가 운영 이미지를 개발 중인
Dockerfile로 다시 빌드하고, runs ID가 같은 운영 컨테이너를 지울 수 있다.
"""

from typing import Final

from app.sandbox.docker_cli import Names

TEST_NAMES: Final = Names(image="pipeline-sandbox-test", container_prefix="pr-proof-test")
