"""PR 흐름에서 환경 처방을 쓴다: 처방 정하기의 결과를 상태의 참조로 바꾸고, 그 참조의 처방으로 PR
head를 돌린다 (DESIGN 4.5 "Python 프로필이 돌릴 것이 없는 저장소").

처방 본문은 상태에 넣지 않고 env_plans에서 id로 다시 읽는다. 행은 지우지 않으므로 재개한 흐름도 같은
처방을 읽는다. 시스템 층과 덮어쓰기 파일은 늘 저장한 처방에서 온다. 의존성 층의 입력만 PR이 바꿀 수
있고, 그 입력은 작성자를 믿을 때만 쓴다(`use_pure.choose_inputs`).
"""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from app.worker.env.install import Limits, default_limits, ensure_layers
from app.worker.env.install_pure import BakeFailed
from app.worker.env.plan import EnvPlan, EnvReady, EnvUnplanned
from app.worker.env.plan_pure import BaseRun, base_run_from_json
from app.worker.env.recipe_pure import EnvRecipe, validate
from app.worker.env.run import open_environment
from app.worker.env.use_pure import (
    EnvPlanned,
    EnvRef,
    EnvSkipped,
    EnvView,
    bake_failed_run,
    choose_inputs,
    unplanned_reason,
    unreadable_reason,
)
from app.worker.git import repo
from app.worker.sandbox import docker_cli
from app.worker.store.state import Store
from app.worker.testing.test_run_pure import EnvUse, TestRun


@dataclass(frozen=True)
class PlannedRun:
    """PR head를 처방으로 돌리는 데 드는 값. trusted는 PR 작성자를 믿는지(8.6)다."""

    plan_id: int
    recipe: EnvRecipe
    base_sha: str
    head_sha: str
    trusted: bool
    limits: Limits


def ref_of(plan: EnvPlan) -> EnvRef:
    match plan:
        case EnvReady(plan_id=plan_id):
            return EnvPlanned(plan_id)
        case EnvUnplanned(kind=kind, reason=reason, rounds=rounds):
            last = rounds[-1].summary if rounds else ""
            return EnvSkipped(unplanned_reason(kind, reason, rounds=len(rounds), last_round=last))


def stored(store: Store, plan_id: int) -> tuple[EnvRecipe, BaseRun] | None:
    """저장한 처방과 그 처방을 확인할 때의 base 실행. 행이 없거나 지금의 검사를 지나지 못하면
    None."""
    entry = store.env_plan(plan_id)
    if entry is None:
        return None
    try:
        recipe = validate(json.loads(entry.recipe_json))
        base_run = base_run_from_json(entry.base_run_json)
    except ValueError:
        return None
    return None if isinstance(recipe, list) else (recipe, base_run)


def prepare(
    store: Store, ref: EnvRef, *, base_sha: str, head_sha: str, trusted: bool
) -> PlannedRun | EnvSkipped:
    """상태의 참조를 돌릴 수 있는 값으로. 상한은 처방을 확인할 때와 같은 기본값이다."""
    match ref:
        case EnvSkipped():
            return ref
        case EnvPlanned(plan_id=plan_id):
            found = stored(store, plan_id)
            if found is None:
                return EnvSkipped(unreadable_reason(plan_id))
            return PlannedRun(plan_id, found[0], base_sha, head_sha, trusted, default_limits())


def view(store: Store, use: EnvUse | None) -> EnvView | None:
    """결과 코멘트의 환경 블록. 처방으로 돌리지 않았으면 None."""
    if use is None:
        return None
    found = stored(store, use.plan_id)
    return None if found is None else EnvView(found[0], found[1], use)


def run_planned(
    planned: PlannedRun,
    worktree: Path,
    *,
    names: docker_cli.Names = docker_cli.DEFAULT_NAMES,
    job_id: str | None = None,
    is_stale: Callable[[], bool] | None = None,
) -> TestRun:
    """층을 갖추고(없는 것만 굽는다) worktree를 그 위에서 빌드·테스트한다.

    층을 굽지 못하면 실행 불가다. docker나 네트워크가 실패하면 SandboxError, git이 실패하면
    GitError가 올라간다.
    """
    recipe = planned.recipe
    inputs = recipe.deps.inputs if recipe.deps is not None else ()
    changed = changed_paths(worktree, planned.base_sha, planned.head_sha, inputs)
    choice = choose_inputs(changed, inputs, trusted=planned.trusted)
    use = EnvUse(planned.plan_id, inputs=choice.source, changed_inputs=choice.changed)
    sha = planned.head_sha if choice.source == "head" else planned.base_sha
    with repo.side_worktree(worktree, sha) as source:
        layers = ensure_layers(recipe, source, planned.limits, names=names)
    if isinstance(layers, BakeFailed):
        return bake_failed_run(layers, use)
    use = replace(use, layers=tuple((layer.name, layer.reused) for layer in layers.layers))
    with open_environment(
        layers, recipe, worktree, planned.limits, names=names, job_id=job_id
    ) as box:
        run = box.run(is_stale=is_stale)
    return replace(run, env=use)


def changed_paths(
    worktree: Path, base_sha: str, head_sha: str, paths: Sequence[str]
) -> tuple[str, ...]:
    """paths 중 두 커밋에서 git 객체가 다른 것(한쪽에만 있는 것 포함)."""
    base = {p: e.sha for p, e in repo.list_base_entries(worktree, base_sha, paths).items()}
    head = {p: e.sha for p, e in repo.list_base_entries(worktree, head_sha, paths).items()}
    return tuple(path for path in paths if base.get(path) != head.get(path))
