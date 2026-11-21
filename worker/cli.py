"""워커 쪽 수동 명령. 웹 쪽 동기화(`sync`)는 `api/cli.py`에 있다.

- `python -m worker.cli reindex [--repo owner/name]`: SQLite의 기록(`records`)으로 벡터 저장소
  (`data_dir/chroma/`)를 다시 만든다 (DESIGN.md 6.5). 벡터 저장소가 지워졌거나 임베딩 모델을 바꿨을
  때 쓴다. 선택 의존성 `[rag]`가 필요하고, 처음이면 임베딩 모델을 받는다.
- `python -m worker.cli eval [--baseline] [--real-model] [--label 이름] [--db 경로] [--out 폴더]`:
  평가 세트 전부(`tests/e2e`의 docker 테스트, 도구를 끈 평가 포함)를 돌려 `eval_runs`에 남긴다
  (DESIGN.md 13절 6단계). `--baseline`이면 기준선으로 남기고, 아니면 같은 종류(가짜 모델 / 실제
  모델)의 가장 최근 기준선과 비교해 보인다. 프롬프트나 모델을 바꾼 뒤 이 명령 하나로 비교한다.
  `--real-model`은 claude를 부른다. `--out`을 주면 그 폴더에 junit, 도구를 끈 평가의 JSON, 실제
  모델의 호출 기록(`memo.jsonl`)을 남긴다. 그 폴더에 이미 `memo.jsonl`이 있으면 그 답을 다시 쓴다.
"""

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Final

from redis import Redis

from shared import config

REPO_ROOT: Final = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m worker.cli")
    parser.add_argument("command", choices=["reindex", "eval"])
    parser.add_argument("--repo", action="append", help="reindex할 저장소 (여러 번 줄 수 있음)")
    parser.add_argument("--baseline", action="store_true", help="eval: 기준선으로 남긴다")
    parser.add_argument("--real-model", action="store_true", help="eval: 실제 claude를 부른다")
    parser.add_argument("--label", default="", help="eval: 이 실행의 이름")
    parser.add_argument("--db", type=Path, help="eval: 저장할 SQLite (기본은 설정의 data_dir)")
    parser.add_argument("--out", type=Path, help="eval: junit·도구를 끈 평가·호출 기록을 남길 폴더")
    args = parser.parse_args(argv)
    if args.command == "eval":
        db = args.db or config.load_config().db_path
        return evaluate(db, baseline=args.baseline, real_model=args.real_model, label=args.label,
                        out=args.out)  # fmt: skip
    return reindex(config.load_config(), args.repo)


def evaluate(db: Path, *, baseline: bool, real_model: bool, label: str, out: Path | None = None,
             runner=subprocess.run) -> int:  # noqa: ANN001  # fmt: skip
    """runner(`subprocess.run`과 같은 꼴)는 테스트가 바꾼다."""
    from worker import eval_run_pure as eval_run
    from worker import evaluation, state

    engine = state.open_engine(db)
    try:
        stored = evaluation.evaluate(engine, REPO_ROOT, baseline=baseline, real_model=real_model,
                                     label=label, runner=runner, out=out)  # fmt: skip
    except evaluation.EvalError as e:
        print(f"평가를 남기지 못함: {e}", file=sys.stderr)
        return 1
    kind = "기준선" if baseline else "실행"
    print(f"eval_runs #{stored.run_id} ({kind}, {'실제 모델' if real_model else '가짜 모델'})")
    print(eval_run.render_summary(stored.summary))
    if not baseline:
        if stored.baseline is None:
            print("비교할 기준선이 없음 (--baseline으로 먼저 남긴다)")
        else:
            comparison = eval_run.compare(evaluation.from_row(stored.baseline), stored.summary)
            print(eval_run.render_comparison(comparison, stored.baseline.id))
    return 0


def reindex(settings: config.PipelineConfig, repos: list[str] | None, encoder=None) -> int:  # noqa: ANN001
    """encoder(`memory.Encoder`)를 주지 않으면 설정의 임베딩 모델을 받아 쓴다. 테스트는 가짜를
    넘긴다."""
    from shared.embed_models_pure import EMBED_MODELS
    from worker import memory
    from worker.state import Store

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
