"""`python -m app.setup.cli <명령>`. 보통은 입구(`npx pr-proof <명령>`, bin/pr-proof.js)가 부른다.

- `start`: api, worker, (smee 주소가 있으면) smee-client를 띄운다 (`app/setup/start.py`)
"""

import argparse
import sys

from app.setup import start


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="npx pr-proof")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("start", help="서버 실행 (api, worker, smee에서 받아 오기)")
    run.add_argument("--node", help="입구가 넘기는 node 실행 파일")
    run.add_argument("--smee", help="입구가 넘기는 smee-client 실행 파일")

    args = parser.parse_args(argv)
    if args.command == "start":
        return start.start(args.node, args.smee)
    return 2


if __name__ == "__main__":
    sys.exit(main())
