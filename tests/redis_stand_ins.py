"""Redis를 보지 않는 테스트가 Redis 자리에 넣는 것.

Redis의 동작(큐 등록, 중복 방지 키, RQ)을 보는 테스트는 이것을 쓰지 않고 tests/conftest.py의
`real_redis`로 실제 Redis에 붙는다.
"""

from typing import Final

# 아무것도 듣지 않는 포트. 이 주소의 실제 클라이언트는 명령마다 연결 오류를 낸다 ("Redis가 꺼짐")
UNREACHABLE_REDIS_URL: Final = "redis://127.0.0.1:1/0"


class MemoryKeys:
    """`app.worker.store.state.Keys`를 채우는 메모리 구현. Store가 쓰는 get·set·delete만 있다.
    값은 실제 Redis처럼 bytes로 돌려준다."""

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def get(self, name: str) -> bytes | None:
        return self.values.get(name)

    def set(self, name: str, value: str | bytes) -> bool:
        self.values[name] = value if isinstance(value, bytes) else str(value).encode()
        return True

    def delete(self, *names: str) -> int:
        return sum(self.values.pop(n, None) is not None for n in names)


class Pong:
    """시작 전 확인(`preflight`)의 Redis 자리. `ping()`만 있고 늘 붙어 있다."""

    def ping(self) -> bool:
        return True
