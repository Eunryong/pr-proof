# 샌드박스 기본 이미지. 언어와 무관한 최소 이미지다 (docs/DESIGN.md 2.3, 8.1).
# 언어의 런타임과 도구(Python, ruff, ast-grep)는 넣지 않는다. 규칙과 테스트가 쓸 때만 고정한 버전과
# SHA256으로 도구 캐시에 받아 읽기 전용으로 붙인다 (app/worker/sandbox/toolcache.py).
# 든 것: glibc(도구 캐시의 gnu 빌드), sh와 coreutils(설치 스크립트, `timeout`), 도구를 받는 curl과
# 인증서(받는 컨테이너만 네트워크가 열린다).
FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work
