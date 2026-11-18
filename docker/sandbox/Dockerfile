# 테스트 샌드박스 베이스 이미지. Python과 규칙 검사 도구(ruff)만 든다.
# 대상 리포의 의존성과 테스트 도구(pytest-cov, pytest-json-report)는 이미지가 아니라
# 캐시 키별 .venv에 설치한다 (docs/DESIGN.md F5).
# ruff는 .venv가 아니라 이미지에 둔다. 대상 리포가 다른 ruff 버전을 의존성에 두어도 충돌하지 않고,
# 의존성 설치가 실패해도 규칙 검사는 할 수 있다.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_INPUT=1

RUN pip install --no-cache-dir ruff==0.17.5

WORKDIR /work
