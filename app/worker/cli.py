"""워커 쪽 수동 명령. 웹 쪽 동기화(`sync`)는 `app/api/cli.py`에 있다.

- `python -m app.worker.cli reindex [--repo owner/name]`: SQLite의 기록(`records`)으로 벡터 저장소
  (`data_dir/chroma/`)를 다시 만든다 (DESIGN.md 6.5). 벡터 저장소가 지워졌거나 임베딩 모델을 바꿨을
  때 쓴다. 선택 의존성 `[rag]`가 필요하고, 처음이면 임베딩 모델을 받는다.
- `python -m app.worker.cli eval [--baseline] [--label 이름] [--db 경로] [--out 폴더]`: 평가 세트
  전부(`tests/e2e`의 docker 테스트, 도구를 끈 평가 포함)를 돌려 `eval_runs`에 남긴다 (DESIGN.md
  13절 6단계). 설정된 모델을 실제로 부른다. `--baseline`이면 기준선으로 남기고, 아니면 가장 최근
  기준선과 비교해 보인다. 프롬프트나 모델을 바꾼 뒤 이 명령 하나로 비교한다.
  `--out`을 주면 그 폴더에 junit, 도구를 끈 평가의 JSON,
  모델의 호출 기록(`memo.jsonl`)을 남긴다. 그 폴더에 이미 `memo.jsonl`이 있으면 그 답을 다시 쓴다.
- `python -m app.worker.cli env-try <처방.json> <저장소 폴더>`: 환경 처방(DESIGN 7.10)을 검사하고
  층을 구워(있으면 다시 쓴다) 그 폴더에서 빌드·테스트를 돌린다. 층, 단계, 상태, 테스트 수를 보이고
  상태가 passed일 때만 0으로 끝난다. 처방이 틀리면 고칠 곳을 한 줄씩 보이고 2로 끝난다.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Final

from redis import Redis

from app.shared import config

REPO_ROOT: Final = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.worker.cli")
    parser.add_argument("command", choices=["reindex", "eval", "env-try"])
    parser.add_argument("paths", nargs="*", type=Path, help="env-try: 처방 JSON과 저장소 폴더")
    parser.add_argument("--repo", action="append", help="reindex할 저장소 (여러 번 줄 수 있음)")
    parser.add_argument("--baseline", action="store_true", help="eval: 기준선으로 남긴다")
    parser.add_argument("--label", default="", help="eval: 이 실행의 이름")
    parser.add_argument("--db", type=Path, help="eval: 저장할 SQLite (기본은 설정의 data_dir)")
    parser.add_argument("--out", type=Path, help="eval: junit·도구를 끈 평가·호출 기록을 남길 폴더")
    args = parser.parse_args(argv)
    if args.command == "env-try":
        if len(args.paths) != 2:
            parser.error("env-try는 처방 JSON과 저장소 폴더를 받는다")
        return env_try(args.paths[0], args.paths[1])
    if args.command == "eval":
        db = args.db or config.load_config().db_path
        return evaluate(db, baseline=args.baseline, label=args.label, out=args.out)
    return reindex(config.load_config(), args.repo)


def evaluate(db: Path, *, baseline: bool, label: str, out: Path | None = None,
             runner=subprocess.run) -> int:  # noqa: ANN001  # fmt: skip
    """runner(`subprocess.run`과 같은 꼴)는 테스트가 바꾼다."""
    from app.worker.evaluation import eval_run_pure, evaluation
    from app.worker.store import state

    engine = state.open_engine(db)
    try:
        stored = evaluation.evaluate(engine, REPO_ROOT, baseline=baseline, label=label,
                                     runner=runner, out=out)  # fmt: skip
    except evaluation.EvalError as e:
        print(f"평가를 남기지 못함: {e}", file=sys.stderr)
        return 1
    kind = "기준선" if baseline else "실행"
    print(f"eval_runs #{stored.run_id} ({kind})")
    print(eval_run_pure.render_summary(stored.summary))
    if not baseline:
        if stored.baseline is None:
            print("비교할 기준선이 없음 (--baseline으로 먼저 남긴다)")
        else:
            comparison = eval_run_pure.compare(evaluation.from_row(stored.baseline), stored.summary)
            print(eval_run_pure.render_comparison(comparison, stored.baseline.id))
    return 0


def env_try(recipe_file: Path, repo: Path) -> int:
    """처방을 검사하고 층을 구워 repo에서 빌드·테스트를 돌린다."""
    from app.worker import env
    from app.worker.env import install_pure
    from app.worker.sandbox.docker_cli import SandboxError

    try:
        data = json.loads(recipe_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"처방을 읽지 못함: {e}", file=sys.stderr)
        return 2
    recipe = env.validate(data)
    if isinstance(recipe, list):
        print("처방에서 고칠 곳:", file=sys.stderr)
        for problem in recipe:
            print(f"- {problem}", file=sys.stderr)
        return 2
    try:
        limits = env.default_limits()
        layers = env.ensure_layers(recipe, repo, limits)
        if isinstance(layers, env.BakeFailed):
            what = "시간 상한을 넘음" if layers.timed_out else f"종료 코드 {layers.exit_code}"
            print(f"층 {layers.layer}을 굽지 못함: {layers.step}, {what}\n{layers.output_tail}")
            return 1
        for layer in layers.layers:
            how = "다시 씀" if layer.reused else f"구움 {layer.seconds:.0f}초"
            print(f"층 {layer.name}: {how} ({layer.image})")
            if not layer.reused:
                hosts = ", ".join(install_pure.contacted(layer.egress)) or "없음"
                print(f"  바깥 연결: {hosts}")
        with env.open_environment(layers, recipe, repo, limits) as box:
            run = box.run()
    except SandboxError as e:
        print(f"환경 오류: {e}", file=sys.stderr)
        return 1
    for step in (*run.build, *((run.test,) if run.test is not None else ())):
        print(f"단계 {step.name}: 종료 코드 {step.exit_code}, {step.seconds:.1f}초")
    print(f"상태: {run.status}")
    if run.unrunnable_reason:
        print(f"까닭: {run.unrunnable_reason}")
    if run.report is not None:
        r = run.report
        print(f"테스트: 전체 {r.total}, 통과 {r.passed}, 실패 {r.failed}, 오류 {r.errors}, "
              f"건너뜀 {r.skipped}")  # fmt: skip
        for name in r.failed_tests:
            print(f"  실패: {name}")
    if run.status != "passed":
        print(run.output[-2000:])
    return 0 if run.status == "passed" else 1


def reindex(settings: config.PipelineConfig, repos: list[str] | None, encoder=None) -> int:  # noqa: ANN001
    """encoder(`memory.Encoder`)를 주지 않으면 설정의 임베딩 모델을 받아 쓴다."""
    from app.shared.embed_models_pure import EMBED_MODELS
    from app.worker.store import memory
    from app.worker.store.state import Store

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
