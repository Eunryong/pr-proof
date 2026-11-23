"""claude --model에 넘기는 모델 이름의 꼴.

저장소의 `.pipeline.yaml`(app/worker/config/repo_config_pure.py)과 파이프라인 쪽 설정의
`model.default`(app/shared/config.py), 설정 명령(app/setup/setup_pure.py)이 같이 쓴다.
"""

from typing import Final

# `-`로 시작하면 옵션으로 읽히므로 막는다
MODEL_NAME_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9._\[\]-]*$"
