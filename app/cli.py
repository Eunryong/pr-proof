"""수동 명령.

- `python -m app.cli sync`: 동기화를 한 번 돌린다. api는 웹훅용 터널로 외부에 열려 있으므로 수동
  동기화를 HTTP 엔드포인트로 만들지 않는다.
- `python -m app.cli reindex [--repo owner/name]`: SQLite의 기록(`records`)으로 벡터 저장소
  (`data_dir/chroma/`)를 다시 만든다 (DESIGN.md 6.5). 벡터 저장소가 지워졌거나 임베딩 모델을 바꿨을
  때 쓴다. 선택 의존성 `[rag]`가 필요하고, 처음이면 임베딩 모델을 받는다.
"""

import argparse
import sys
from pathlib import Path

from redis import Redis

from app import config
from app.controllers import sync
from app.services.history import History


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    parser.add_argument("command", choices=["sync", "reindex"])
    parser.add_argument("--repo", action="append", help="reindex할 저장소 (여러 번 줄 수 있음)")
    args = parser.parse_args(argv)
    settings = config.load_config()
    if args.command == "reindex":
        return reindex(settings, args.repo)
    result = sync.run_once(settings, Redis.from_url(settings.redis_url), History(settings.db_path))
    for repo, number, reason in result.enqueued:
        print(f"등록: {repo}#{number} ({reason})")
    for repo in result.failed_repos:
        print(f"조회 실패: {repo}", file=sys.stderr)
    if not result.enqueued and not result.failed_repos:
        print("등록할 작업 없음")
    return 1 if result.failed_repos else 0


def reindex(settings: config.PipelineConfig, repos: list[str] | None, encoder=None) -> int:  # noqa: ANN001
    """encoder(`memory.Encoder`)를 주지 않으면 설정의 임베딩 모델을 받아 쓴다. 테스트는 가짜를
    넘긴다."""
    from app.models.rag import EMBED_MODELS
    from app.worker import memory
    from app.worker.state import Store

    store = Store(settings.db_path, Redis.from_url(settings.redis_url))
    try:
        if encoder is None:
            spec = EMBED_MODELS[settings.rag.embed_model]
            folder = memory.ensure_model(Path(settings.data_dir) / memory.MODEL_DIR, spec)
            encoder = memory.OnnxEncoder(spec, folder)
        spec = encoder.spec
        vectors = memory.VectorStore(Path(settings.data_dir) / memory.CHROMA_DIR, spec.name,
                                     spec.dim)  # fmt: skip
        counts = memory.reindex(store, vectors, encoder, repos)
    except (memory.SearchUnavailable, ImportError) as e:
        print(f"reindex하지 못함: {e}", file=sys.stderr)
        return 1
    for repo, n in counts.items():
        print(f"{repo}: 기록 {n}건")
    if not counts:
        print("기록 없음")
    return 0


if __name__ == "__main__":
    sys.exit(main())
