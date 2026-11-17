# 테스트 샌드박스 베이스 이미지. Python만 든다.
# 대상 리포의 의존성과 검사 도구(pytest-cov, pytest-json-report)는 이미지가 아니라
# 캐시 키별 .venv에 설치한다 (docs/DESIGN.md F5).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_INPUT=1

WORKDIR /work
