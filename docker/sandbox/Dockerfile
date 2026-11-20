# 테스트 샌드박스 베이스 이미지. Python만 든다.
# 대상 리포의 의존성과 테스트 도구(pytest-cov, pytest-json-report)는 이미지가 아니라
# 캐시 키별 .venv에 설치한다 (docs/DESIGN.md 4.5).
# ruff는 이미지에 넣지 않는다. 규칙이 ruff를 쓸 때만 저장소가 고정한 버전을 도구 캐시에 받는다
# (docs/DESIGN.md 2.3, app/sandbox/toolcache.py).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_INPUT=1

WORKDIR /work
