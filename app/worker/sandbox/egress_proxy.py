"""굽는 컨테이너의 바깥 연결 프록시. 호스트가 아니라 프록시 컨테이너 안에서 돈다 (DESIGN 8.1, T41).

이미지는 `docker/proxy/Dockerfile`이고 이 파일과 `egress_pure.py`만 넣는다. 표준 라이브러리와
`egress_pure`만 import한다. 요청마다 이름을 풀어 모든 주소가 공용일 때만 확인한 주소에 연결하고,
판정을 `egress_pure.LOG_PATH`에 한 줄씩 남긴다. 줄은 요청에 답하기 전에 쓴다. 호스트가 단계가
끝난 뒤 읽을 때 빠진 줄이 없다.
"""

import asyncio
import os
import socket
from collections.abc import Mapping
from typing import IO, Final

from app.worker.sandbox.egress_pure import (
    LOG_PATH,
    MAX_HEAD_BYTES,
    PORT,
    BadRequest,
    Denied,
    Event,
    Request,
    Verdict,
    parse_request,
    public_addresses,
)

HEAD_TIMEOUT_S: Final = 60
CONNECT_TIMEOUT_S: Final = 30
_CHUNK: Final = 64 * 1024
# 이름이 없다는 답. 그 밖의 getaddrinfo 오류(EAI_AGAIN 등)는 이름 서버에 닿지 못한 것이다
_NO_NAME: Final = frozenset(
    code for code in (socket.EAI_NONAME, getattr(socket, "EAI_NODATA", None)) if code is not None
)
_STATUS: Final[Mapping[str, bytes]] = {
    "denied": b"403 Forbidden",
    "bad_request": b"400 Bad Request",
}

Upstream = tuple[asyncio.StreamReader, asyncio.StreamWriter]


async def open_upstream(host: str, port: int) -> tuple[Verdict, str, Upstream | None]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        return ("unresolved" if e.errno in _NO_NAME else "dns_failed"), str(e), None
    checked = public_addresses([str(info[4][0]) for info in infos])
    if isinstance(checked, Denied):
        return "denied", checked.reason, None
    refused: str | None = None
    unreachable = ""
    for address in checked:
        try:
            upstream = await asyncio.wait_for(
                asyncio.open_connection(address, port), CONNECT_TIMEOUT_S
            )
        except ConnectionRefusedError as e:
            refused = f"{address}: {e}"
        except (TimeoutError, OSError) as e:
            unreachable = f"{address}: {e or type(e).__name__}"
        else:
            return "allowed", address, upstream
    if refused is not None:
        return "refused", refused, None
    return "unreachable", unreachable, None


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, log: IO[str]) -> None:
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT_S)
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError, OSError):
        writer.close()
        return
    request = parse_request(head)
    if isinstance(request, BadRequest):
        _record(log, Event("bad_request", "", "", 0, request.reason))
        await _refuse(writer, "bad_request")
        return
    verdict, detail, upstream = await open_upstream(request.host, request.port)
    _record(log, Event(verdict, request.method, request.host, request.port, detail))
    if upstream is None:
        await _refuse(writer, verdict)
        return
    await _relay(request, reader, writer, *upstream)


async def _relay(
    request: Request,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    up_reader: asyncio.StreamReader,
    up_writer: asyncio.StreamWriter,
) -> None:
    try:
        if request.method == "CONNECT":
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
            return
        body = await reader.readexactly(request.body_length) if request.body_length else b""
        up_writer.write(request.forward + body)
        await up_writer.drain()
        await _pipe(up_reader, writer)
    except (asyncio.IncompleteReadError, OSError):
        pass
    finally:
        up_writer.close()
        writer.close()


async def _pipe(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
    try:
        while data := await source.read(_CHUNK):
            sink.write(data)
            await sink.drain()
    except OSError:
        pass
    finally:
        sink.close()


async def _refuse(writer: asyncio.StreamWriter, verdict: Verdict) -> None:
    status = _STATUS.get(verdict, b"502 Bad Gateway")
    try:
        writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
    except OSError:
        pass
    finally:
        writer.close()


def _record(log: IO[str], event: Event) -> None:
    log.write(event.line() + "\n")
    log.flush()


async def main() -> None:
    starting = LOG_PATH + ".starting"
    with open(starting, "a", encoding="utf-8") as log:
        server = await asyncio.start_server(
            lambda r, w: handle(r, w, log), "0.0.0.0", PORT, limit=MAX_HEAD_BYTES
        )
        # 호스트는 LOG_PATH가 생기면 준비된 것으로 본다. 그래서 받기 시작한 뒤에 이름을 바꾼다
        os.rename(starting, LOG_PATH)
        async with server:
            await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
