# 굽는 컨테이너의 바깥 연결 프록시 (docs/design/08-security.md 8.1, 위협 T41).
# 코드는 app/worker/sandbox/의 egress_proxy.py와 egress_pure.py 둘이고 빌드할 때 `src` 문맥으로 받는다
# (app/worker/sandbox/egress.py). 호스트의 Python(3.11)과 같은 판이라 주소 판정의 단위 테스트가
# 컨테이너 안의 판정과 같다.
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /opt/proxy
COPY --from=src egress_pure.py egress_proxy.py /opt/proxy/app/worker/sandbox/
USER 65534:65534
CMD ["python", "-m", "app.worker.sandbox.egress_proxy"]
