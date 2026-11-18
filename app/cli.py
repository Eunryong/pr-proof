"""수동 명령. `python -m app.cli sync`로 동기화를 한 번 돌린다.

api는 웹훅용 터널로 외부에 열려 있으므로 수동 동기화를 HTTP 엔드포인트로 만들지 않는다.
"""

import argparse
import sys

from redis import Redis

from app import config
from app.controllers import sync
from app.services.history import History


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    parser.add_argument("command", choices=["sync"])
    parser.parse_args(argv)
    settings = config.load_config()
    result = sync.run_once(settings, Redis.from_url(settings.redis_url), History(settings.db_path))
    for repo, number, reason in result.enqueued:
        print(f"등록: {repo}#{number} ({reason})")
    for repo in result.failed_repos:
        print(f"조회 실패: {repo}", file=sys.stderr)
    if not result.enqueued and not result.failed_repos:
        print("등록할 작업 없음")
    return 1 if result.failed_repos else 0


if __name__ == "__main__":
    sys.exit(main())
