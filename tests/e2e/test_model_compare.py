"""수정 작성의 모델 구성 비교 (docs/DESIGN.md 9.3, 13절 6단계, 완료 기준 6-2).

세 구성으로 같은 케이스를 돈다. `.pipeline.yaml`의 `model.light`·`model.heavy`만 바꾼다.

- `light_only`: 가벼운 모델만 (light·heavy 모두 haiku). 세 번째 회차도 haiku다.
- `heavy_only`: 무거운 모델만 (light·heavy 모두 sonnet).
- `light_then_heavy`: 기본값 (haiku로 시작하고 두 번 실패하면 sonnet으로 한 번 올린다).

케이스는 `rules/09`, `10`, `26`, `30`, `31`이다. `31`은 음수에서도 짝수 쪽 반올림을 지켜야 해서
가벼운 모델이 두 번 연이어 실패하면 무거운 모델로 올라가는지 보는 표본이다.
`PR_PROOF_MODEL_COMPARE_CASES`(쉼표)로 고를 수 있다.

규칙 읽기·검사기 작성·수정 판단은 케이스의 답(가짜)이고 수정 작성만 실제 모델이다. 그래서 세 구성의
차이는 수정 작성의 모델 하나다. 매번 빈 데이터 폴더다(저장된 답이 구성 사이에 섞이지 않게).

- 해소율: 수정안이 검증(4.8: 규칙 재검사, 테스트, 허용 범위)을 통과해 수정안 PR까지 간 비율.
- 채택률: 수정안이 케이스가 정한 팀의 방식(`ADOPTED`)과 맞는 비율 (6.7의 평가 세트 정의).
- 호출 수: 수정 작성의 실제 모델 호출 수(`ai_calls`의 줄), 상한에서 센 값(회차의 단계가 heavy면 3),
  모델로 다시 센 값(sonnet 3, haiku 1. 9.2의 "무거운 모델 호출은 3").

기본은 가짜 수정 모델로 구성마다 한 번 돌아 하네스만 본다. `PR_PROOF_REAL_MODEL=1`이면 수정 작성만
로그인된 claude를 부르고 `PR_PROOF_MODEL_COMPARE_RUNS`(기본 1)번씩 돈다.
`PR_PROOF_MODEL_COMPARE_OUT`이 있으면 결과 JSON을 쓴다. 없으면 평가 명령의 출력 폴더
(`PR_PROOF_TOOLS_EVAL_OUT`)에 쓴다.
"""

import ast
import json
import os
import textwrap
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final

import pytest
import yaml
from sqlmodel import Session, select

from app.shared import config
from app.shared.db_pure import FixAttempt
from app.worker.ai import claude_cli
from app.worker.deps import Deps
from app.worker.evaluation import TOOLS_OFF_OUT_ENV
from app.worker.git import repo
from app.worker.git.github import GitHub
from app.worker.store.state import Store
from tests.sandbox_names import TEST_NAMES

from ..redis_stand_ins import MemoryKeys
from ..worker.git.test_github import FakeGh
from .test_lint_cases import _git, shared_venvs  # noqa: F401
from .test_rules_cases import (
    BRANCH,
    CASES_DIR,
    REAL_MODEL_ENV,
    _check,
    _remote,
    scripted_fixer,
    scripted_model,
)

RUNS_ENV: Final = "PR_PROOF_MODEL_COMPARE_RUNS"
OUT_ENV: Final = "PR_PROOF_MODEL_COMPARE_OUT"
CASES_ENV: Final = "PR_PROOF_MODEL_COMPARE_CASES"  # 쉼표로 고른 케이스만 돈다
REPO: Final = "o/r"
# 긴 실제 모델 실행이 같은 Docker의 다른 docker 테스트와 작업 컨테이너 이름(runs ID)이 겹치지 않게
_PREFIX: Final = f"{TEST_NAMES.container_prefix}-cmp{os.getpid()}"
FIXED_FILE: Final = "orders/services/pricing.py"
ARMS: Final[Mapping[str, Mapping[str, str]]] = {
    "light_only": {"light": "haiku", "heavy": "haiku"},
    "heavy_only": {"light": "sonnet", "heavy": "sonnet"},
    "light_then_heavy": {"light": "haiku", "heavy": "sonnet"},
}


def _discount_body(text: str, name: str = "discount") -> str:
    start = text.index(f"def {name}(")
    end = text.find("\ndef ", start + 1)
    return text[start : end if end > 0 else len(text)]


def _integer_only(source: str) -> bool:
    for node in ast.walk(ast.parse(textwrap.dedent(source))):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return False
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") in {"round", "float"}:
            return False
    return True


# 케이스마다 팀의 방식. 검증을 통과한 수정안의 고친 파일을 받는다
ADOPTED: Final[Mapping[str, Callable[[str], bool]]] = {
    # 두 줄 모두 원래 있던 상수를 다시 쓴다 (가장 작은 변경, 2026-11-21)
    "09-old-static-01-magic-value-fix": lambda text: (
        "order.coupon_percent > MAX_COUPON_PERCENT" in text
        and "discount(order) >= FREE_SHIPPING_MIN" in text
    ),
    # 모듈 수준 상태를 없앤다 (인자로 넘기거나 다른 이름으로 옮기지 않는다)
    "10-old-static-02-global-state-fix": lambda text: "_seen_orders" not in text,
    # float·round·`/` 없이 정수 연산으로 반올림을 지킨다 (주석의 글은 보지 않는다)
    "26-money-rounding-fix": lambda text: _integer_only(_discount_body(text)),
    "30-money-half-even-fix": lambda text: _integer_only(_discount_body(text)),
    "31-money-signed-half-even-fix": lambda text: _integer_only(_discount_body(text, "adjustment")),
}


def _real() -> bool:
    return os.environ.get(REAL_MODEL_ENV) == "1"


def _case(name: str, arm: str) -> dict:
    case = yaml.safe_load((CASES_DIR / f"{name}.yaml").read_text())
    case["pipeline"] = {**(case.get("pipeline") or {}), "model": dict(ARMS[arm])}
    return case


def _deps(remote: dict, data: Path, shared_venvs: Path, tool_cache: Path, monkeypatch):  # noqa: F811
    data.mkdir()
    (data / "venvs").symlink_to(shared_venvs, target_is_directory=True)
    (data / "tools").symlink_to(tool_cache, target_is_directory=True)
    monkeypatch.setattr(repo, "remote_url", lambda name: str(remote["path"]))
    redis = MemoryKeys()
    gh = FakeGh(head=remote["head"], base=remote["base"])
    settings = config.PipelineConfig(
        repos=[REPO], data_dir=data,
        sandbox=config.SandboxConfig(image=TEST_NAMES.image,
                                     container_prefix=_PREFIX),
    )  # fmt: skip
    return Deps(Store(data / "pipeline.db", redis), GitHub(gh), settings), gh, redis


def run_once(name: str, arm: str, n: int, tmp: Path, eval_repo: Path, shared_venvs,  # noqa: F811
             tool_cache: Path, monkeypatch) -> dict:  # fmt: skip
    case = _case(name, arm)
    monkeypatch.setattr(claude_cli, "run_structured", scripted_model(case))
    if not _real():
        monkeypatch.setattr(claude_cli, "run_claude", scripted_fixer(case))
    folder = tmp / f"{name}-{arm}-{n}"
    folder.mkdir()
    remote = _remote(folder, eval_repo, case)
    deps, gh, redis = _deps(remote, folder / "data", shared_venvs, tool_cache, monkeypatch)
    _check(deps, gh, redis, remote, pr=1)
    with Session(deps.store.engine) as session:
        attempts = list(session.exec(select(FixAttempt).where(FixAttempt.repo == REPO)))
    fix_rows = [c for c in deps.store.model_calls(REPO, 1) if c.stage == "fix" and not c.cache_hit]
    pushed = ""
    if _git(remote["path"], "branch", "--list", BRANCH):
        pushed = _git(remote["path"], "show", f"{BRANCH}:{FIXED_FILE}")
    return {
        "case": name, "arm": arm, "run": n,
        "resolved": bool(pushed),
        "adopted": bool(pushed) and ADOPTED[name](pushed),
        "fix_calls": len(fix_rows),
        "fix_counted": sum(c.counted for c in fix_rows),
        "fix_units": sum(3 if c.model == "sonnet" else 1 for c in fix_rows),
        "fix_failed_calls": sum(1 for c in fix_rows if c.ok is False),
        "models": [c.model_id or c.model for c in fix_rows],
        "tiers": [c.tier for c in fix_rows],
        "status": [a.status for a in attempts],
        "detail": [a.detail[:300] for a in attempts],
        "attempts": [a.attempts for a in attempts],
        "tokens": [(c.input_tokens, c.output_tokens) for c in fix_rows],
        "pushed_discount": _discount_body(pushed) if "def discount(" in pushed else "",
    }  # fmt: skip


def _chosen() -> list[str]:
    picked = [c for c in os.environ.get(CASES_ENV, "").split(",") if c]
    unknown = set(picked) - set(ADOPTED)
    if unknown:
        raise ValueError(f"모르는 케이스: {sorted(unknown)}")
    return picked or list(ADOPTED)


def summarize(runs: list[dict]) -> list[dict]:
    """구성마다 해소율·채택률·호출 수. 비율은 "맞은 수/전체"로 둔다."""
    out = []
    for arm in ARMS:
        rows = [r for r in runs if r["arm"] == arm]
        if not rows:
            continue
        out.append({
            "arm": arm, "runs": len(rows),
            "resolved": f"{sum(r['resolved'] for r in rows)}/{len(rows)}",
            "adopted": f"{sum(r['adopted'] for r in rows)}/{len(rows)}",
            "fix_calls": sum(r["fix_calls"] for r in rows),
            "fix_counted": sum(r["fix_counted"] for r in rows),
            "fix_units": sum(r["fix_units"] for r in rows),
            "by_case": {
                case: f"{sum(r['resolved'] for r in rows if r['case'] == case)}"
                      f"/{sum(r['adopted'] for r in rows if r['case'] == case)}"
                      f"/{sum(1 for r in rows if r['case'] == case)}"
                for case in ADOPTED
            },
        })  # fmt: skip
    return out


def test_summarize_counts_each_arm_separately() -> None:
    runs = [
        {"case": "26-money-rounding-fix", "arm": "light_only", "resolved": True, "adopted": False,
         "fix_calls": 2, "fix_counted": 2, "fix_units": 2},
        {"case": "26-money-rounding-fix", "arm": "light_only", "resolved": False,
         "adopted": False, "fix_calls": 3, "fix_counted": 5, "fix_units": 3},
        {"case": "26-money-rounding-fix", "arm": "heavy_only", "resolved": True, "adopted": True,
         "fix_calls": 1, "fix_counted": 1, "fix_units": 3},
    ]  # fmt: skip
    by_arm = {row["arm"]: row for row in summarize(runs)}
    assert set(by_arm) == {"light_only", "heavy_only"}
    assert by_arm["light_only"]["resolved"] == "1/2" and by_arm["light_only"]["adopted"] == "0/2"
    assert by_arm["light_only"]["fix_counted"] == 7
    assert by_arm["heavy_only"]["by_case"]["26-money-rounding-fix"] == "1/1/1"
    assert by_arm["heavy_only"]["by_case"]["09-old-static-01-magic-value-fix"] == "0/0/0"


def test_adopted_reads_the_teams_way() -> None:
    rounding = ADOPTED["26-money-rounding-fix"]
    good = "def discount(o):\n    return (a * p + 50) // 100\n\n\ndef total(o):\n    return 1\n"
    assert rounding(good)
    assert not rounding(good.replace("(a * p + 50) // 100", "round(a * p // 100)"))
    assert not rounding(good.replace("(a * p + 50) // 100", "int(a * p / 100)"))
    # divmod도 정수 연산이고, 주석에 쓴 round()는 코드가 아니다
    assert rounding("def discount(o):\n    q, r = divmod(a, 100)  # round()와 같음\n    return q\n")
    assert ADOPTED["10-old-static-02-global-state-fix"]("x = 1\n")
    assert not ADOPTED["10-old-static-02-global-state-fix"]("_seen_orders = ()\n")


@pytest.mark.docker
def test_model_arms(eval_repo: Path, shared_venvs, tool_cache: Path, tmp_path: Path,  # noqa: F811
                    monkeypatch) -> None:  # fmt: skip
    runs_n = int(os.environ.get(RUNS_ENV, "1")) if _real() else 1
    runs = [
        run_once(name, arm, n, tmp_path, eval_repo, shared_venvs, tool_cache, monkeypatch)
        for n in range(runs_n) for name in _chosen() for arm in ARMS
    ]  # fmt: skip
    table = summarize(runs)
    if folder := os.environ.get(OUT_ENV) or os.environ.get(TOOLS_OFF_OUT_ENV):
        out = Path(folder)
        out.mkdir(parents=True, exist_ok=True)
        (out / "model_compare.json").write_text(
            json.dumps({"summary": table, "runs": runs}, ensure_ascii=False, indent=1)
        )
    for row in table:
        print(row)
    if _real():
        return
    # 가짜 수정 모델은 구성과 관계없이 같은 편집을 하므로 세 구성의 결과가 같고, 반올림 케이스는
    # 1회차에 테스트가 깨져 2회차에 통과한다
    for row in table:
        assert row["resolved"] == row["adopted"] == "5/5", row
    rounding = [r for r in runs if r["case"] == "26-money-rounding-fix"]
    assert all(r["fix_calls"] == 2 and r["attempts"] == [2] for r in rounding), rounding
    # 짝수 쪽 반올림 케이스는 두 번 실패한 뒤 3회차에 통과한다. 3회차는 무거운 단계다
    for name in ("30-money-half-even-fix", "31-money-signed-half-even-fix"):
        half_even = {r["arm"]: r for r in runs if r["case"] == name}
        assert all(r["attempts"] == [3] and r["tiers"] == ["light", "light", "heavy"]
                   for r in half_even.values()), half_even  # fmt: skip
        assert half_even["light_then_heavy"]["models"] == ["haiku", "haiku", "sonnet"]
        assert half_even["light_only"]["models"] == ["haiku"] * 3
    by_arm = {r["arm"]: r for r in rounding}
    # 상한은 회차의 단계(light·heavy)로 센다. 무거운 모델만 구성의 sonnet도 1회차·2회차는 가벼운
    # 단계라 1로 센다. 9.2의 "무거운 모델 3"으로 다시 센 값이 fix_units다
    assert by_arm["heavy_only"]["fix_counted"] == by_arm["light_only"]["fix_counted"] == 2
    assert by_arm["heavy_only"]["fix_units"] == 6 and by_arm["light_only"]["fix_units"] == 2
