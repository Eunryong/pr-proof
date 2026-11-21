"""claude CLI가 띄우는 stdio MCP 서버: 줄을 워커의 Unix 소켓으로 넘기고 답을 돌려준다.

파이프라인이 `python -I mcp_relay.py <소켓>`으로 `--mcp-config`에 적는다 (docs/DESIGN.md 4.7, 8.4).
도구의 뜻과 실행은 모두 워커(`app/worker/ai/tool_server.py`)에 있고, 이 파일은 메시지를 읽지 않는다.
CLI가 따로 실행하므로 표준 라이브러리만 쓴다.
"""

import socket
import sys
import threading


def main(argv: list[str]) -> int:
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(argv[1])
    except (IndexError, OSError) as e:
        print(f"파이프라인 도구 서버에 붙지 못함: {type(e).__name__}", file=sys.stderr)
        return 1

    def upstream() -> None:
        try:
            for line in sys.stdin.buffer:
                sock.sendall(line)
        except OSError:
            pass
        finally:
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    threading.Thread(target=upstream, daemon=True).start()
    with sock.makefile("rb") as replies:
        for line in replies:
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
