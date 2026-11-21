"""수정 작성의 모델이 부르는 파이프라인 도구의 서버 (docs/DESIGN.md 4.7, 8.4).

claude 호출 하나 동안 워커 안에 Unix 소켓을 열고, CLI가 띄운 `mcp_relay.py`가 넘기는 MCP 요청에
답한다. 도구의 구현은 부르는 쪽이 넘기는 함수(파이프라인의 코드)다. 모델에게 명령 실행을 주지
않는다. 도구는 한 번에 하나씩 돈다(같은 worktree와 샌드박스를 쓴다). 호출 수와 시간이 상한에 닿으면
도구를 돌리지 않고 "상한에 닿음"을 돌려주며, claude 호출은 정상으로 끝난다.
"""

import json
import logging
import socket
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from app.worker import mcp_pure
from app.worker.mcp_pure import ToolLimits, ToolUsage

RELAY: Final = Path(__file__).with_name("mcp_relay.py")
# 도구 하나의 결과 글을 만드는 함수. 인자는 `mcp_pure.tool_arguments`가 좁힌 것이다
Handler = Callable[[Mapping[str, str]], str]

log = logging.getLogger(__name__)


@dataclass
class _Counter:
    limits: ToolLimits
    counts: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0
    limited: bool = False

    def usage(self) -> ToolUsage:
        return ToolUsage(
            check_rules=self.counts.get(mcp_pure.CHECK_RULES, 0),
            run_tests=self.counts.get(mcp_pure.RUN_TESTS, 0),
            seconds=round(self.seconds, 1),
            limited=self.limited,
        )


class ToolServer:
    """열려 있는 동안 도구 요청에 답한다. `config`를 claude의 `--mcp-config`로 넘긴다."""

    def __init__(self, sock_path: Path, handlers: Mapping[str, Handler], limits: ToolLimits):
        self.sock_path = sock_path
        self._handlers = dict(handlers)
        self._counter = _Counter(limits)
        self._run_lock = threading.Lock()
        self.config = mcp_pure.mcp_config(sys.executable, ["-I", str(RELAY), str(sock_path)])

    def usage(self) -> ToolUsage:
        return self._counter.usage()

    def call(self, name: str, args: Mapping[str, str]) -> tuple[str, bool]:
        """도구 하나를 돌린다. 상한에 닿았으면 돌리지 않는다. 도구가 실패해도 글로 돌려준다."""
        with self._run_lock:
            c = self._counter
            reason = mcp_pure.limit_reason(sum(c.counts.values()), c.seconds, c.limits)
            if reason is not None:
                c.limited = True
                return reason, False
            handler = self._handlers.get(name)
            if handler is None:
                return f"이 호출에서 쓸 수 없는 도구: {name}", True
            c.counts[name] = c.counts.get(name, 0) + 1
            started = time.monotonic()
            try:
                return handler(args), False
            except Exception as e:  # 도구가 실패해도 claude 호출은 이어 간다. 출구 검사가 다시 본다
                log.warning("파이프라인 도구 %s 실패: %s", name, e)
                return f"도구를 돌리지 못함: {type(e).__name__}", True
            finally:
                c.seconds += time.monotonic() - started

    def serve(self, conn: socket.socket) -> None:
        write_lock = threading.Lock()

        def answer(line: str) -> None:
            reply = mcp_pure.respond(line, self.call)
            if reply is None:
                return
            data = (json.dumps(reply, ensure_ascii=False) + "\n").encode()
            with write_lock:
                try:
                    conn.sendall(data)
                except OSError:
                    pass

        workers = []
        with conn, conn.makefile("rb") as lines:
            for raw in lines:
                worker = threading.Thread(target=answer, args=(raw.decode(errors="replace"),))
                worker.start()
                workers.append(worker)
            for worker in workers:
                worker.join()


@contextmanager
def serving(
    handlers: Mapping[str, Handler], limits: ToolLimits | None = None
) -> Iterator[ToolServer]:
    """claude 호출 하나 동안 도구 서버를 연다. 소켓은 이 호출만의 임시 폴더에 둔다."""
    with tempfile.TemporaryDirectory(prefix="pp-tools-") as tmp:
        sock_path = Path(tmp) / "s"
        server = ToolServer(sock_path, handlers, limits or ToolLimits())
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(sock_path))
        sock_path.chmod(0o600)
        listener.listen(4)
        listener.settimeout(0.5)  # 닫을 때 accept가 깨어나도록
        stop = threading.Event()

        def accept() -> None:
            while not stop.is_set():
                try:
                    conn, _ = listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    return
                conn.settimeout(None)
                threading.Thread(target=server.serve, args=(conn,), daemon=True).start()

        thread = threading.Thread(target=accept, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            stop.set()
            listener.close()
            thread.join(timeout=5)
