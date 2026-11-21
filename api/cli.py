"""웹 쪽 수동 명령.

- `python -m api.cli sync`: 동기화를 한 번 돌린다. api는 웹훅용 터널로 외부에 열려 있으므로 수동
  동기화를 HTTP 엔드포인트로 만들지 않는다.

워커 쪽 수동 명령(reindex, eval)은 `worker/cli.py`에 있다.
"""

import argparse
import sys

from redis import Redis

from api import sync
from api.history import History
from shared import config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m api.cli")
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
