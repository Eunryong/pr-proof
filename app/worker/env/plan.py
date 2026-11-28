"""환경 처방 정하기 (DESIGN 4.5 "환경 처방 정하기"): 모델이 저장소의 base 커밋을 읽고 처방을 쓰고,
프로그램이 그 처방을 base에서 굽고 돌려 확인한다. 틀리면 그 결과를 다음 회차에 주고 고치게 한다.

모델은 base 커밋의 side worktree를 복사한 모델 폴더를 읽기 도구로 읽는다(`ModelBackend.reading`,
claude 구현은 Read·Grep·Glob만, 8.4). 읽을 파일은 모델이 스스로 찾는다. 프로그램이 "빌드 파일
이름"을 골라 주면 그것이 새 고정 목록이 되고, 명령이 문서나 스크립트에만 적힌 저장소에서 명령을
놓친다.
PR의 diff, 설명, 코멘트는 이 단계에 주지 않는다.

회차는 공용 틀(`model/loop.py`)의 write → verify다. write는 모델 호출 하나, verify는 처방 검사, 층
굽기(`install.ensure_layers`), base에서 빌드·테스트(`run.open_environment`)다. 확인한 처방은
`env_plans`에 기댄 경로의 git 객체 해시와 함께 저장하고, 그 경로가 모두 그대로인 base에서는 모델을
부르지 않고 다시 쓴다. 확인하지 못한 처방, 호출 상한, 모델 호출 실패는 저장하지 않는다. 굽는 중의
네트워크 실패와 docker 오류(`SandboxError`)는 환경 오류라 회차로 세지 않고 그대로 올라간다.
"""

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from app.shared.db_pure import EnvPlanEntry
from app.worker.env.install import Limits, ensure_layers
from app.worker.env.install_pure import BakeFailed
from app.worker.env.plan_pure import (
    ANSWER_SCHEMA,
    CALL_BUDGET_USD,
    MAX_ROUNDS,
    MAX_TOOL_USES,
    PROMPT_VERSION,
    RED_NOT_ALLOWED,
    SYSTEM_PROMPT,
    Answer,
    BaseIsRed,
    BaseRun,
    NothingToRun,
    Platform,
    Previous,
    RecipeAnswer,
    UnplannedKind,
    bake_feedback,
    base_run_of,
    base_run_to_json,
    build_prompt,
    failing_step,
    is_fresh,
    judge_base_run,
    plan_reads,
    problems_feedback,
    read_answer,
    reads_problems,
    run_feedback,
    stored_plan,
)
from app.worker.env.recipe_pure import EnvRecipe, recipe_json, validate
from app.worker.env.run import open_environment
from app.worker.git import repo
from app.worker.git.git_pure import TreeEntry
from app.worker.model.loop import (
    LoopPolicy,
    LoopState,
    Verdict,
    WriteFailed,
    WriteResult,
    build_loop,
)
from app.worker.model.model_backend import AskModel, ReadCode
from app.worker.sandbox import docker_cli
from app.worker.store.budget import CallBudget
from app.worker.store.call_budget_pure import Denied, EnvScope
from app.worker.store.state import Store
from app.worker.testing.test_run_pure import TestRun

log = logging.getLogger(__name__)

_TREE_MODE = "040000"


@dataclass(frozen=True)
class Round:
    """회차 하나의 기록. feedback은 다음 회차에 준 글이고, 확인을 통과했으면 비었다."""

    number: int
    summary: str
    feedback: str = ""


@dataclass(frozen=True)
class EnvReady:
    """base에서 확인한 처방. base_run.base_is_red가 있으면 처방은 맞고 base 자체가 빨갛다."""

    plan_id: int
    recipe: EnvRecipe
    base_run: BaseRun
    reads: Mapping[str, str]  # 기댄 경로 → base의 git 객체 해시
    cached: bool
    rounds: tuple[Round, ...] = ()


@dataclass(frozen=True)
class EnvUnplanned:
    """처방이 없다. unverified면 reason이 마지막 회차의 확인 결과다."""

    kind: UnplannedKind
    reason: str
    rounds: tuple[Round, ...] = ()


EnvPlan = EnvReady | EnvUnplanned


def plan_environment(
    worktree: Path,
    base_sha: str,
    *,
    store: Store,
    budget: CallBudget,
    model: str,
    read_code: ReadCode,
    limits: Limits,
    names: docker_cli.Names = docker_cli.DEFAULT_NAMES,
) -> EnvPlan:
    """base_sha의 처방을 돌려준다. worktree는 그 저장소의 worktree(base의 side worktree를 여기서
    만든다), model은 `model.env`다. 저장한 처방이 그대로 쓸 만하면 모델을 부르지 않는다.

    docker나 네트워크가 실패하면 SandboxError, git이 실패하면 GitError를 올린다.
    """
    scope = EnvScope(budget.repo)
    saved = _saved(worktree, base_sha, store=store, repo_name=budget.repo, model=model)
    if saved is not None:
        budget.record_hit(scope, stage="env", tier="light", model=model)
        return saved
    denied = budget.decide(scope, "light")
    if isinstance(denied, Denied):
        return EnvUnplanned("cap", denied.reason)
    root = _root_listing(repo.base_root_entries(worktree, base_sha))
    platform = Platform(
        arch=docker_cli.run("info", "--format", "{{.Architecture}}").stdout.strip(),
        cpus=limits.cpus,
        memory_gb=limits.memory_gb,
        install_timeout_s=limits.install_timeout_s,
        step_timeout_s=limits.step_timeout_s,
    )
    with repo.side_worktree(worktree, base_sha) as side, read_code(side) as ask:
        planner = _Planner(side, base_sha, ask, root, platform, store=store, budget=budget,
                           model=model, limits=limits, names=names)  # fmt: skip
        return planner.run()


def _saved(
    worktree: Path, base_sha: str, *, store: Store, repo_name: str, model: str
) -> EnvReady | None:
    """그 저장소의 처방 중 최신부터, 모델·프롬프트 판이 같고 기댄 경로가 모두 그대로인 것."""
    for entry in store.env_plans(repo_name):
        plan = stored_plan(
            model=entry.model, prompt_version=entry.prompt_version, recipe_json=entry.recipe_json,
            reads_json=entry.reads_json, base_run_json=entry.base_run_json,
        )  # fmt: skip
        if plan is None:
            log.warning("저장한 처방 #%s을 읽지 못해 건너뜀", entry.id)
            continue
        blobs = _blobs(worktree, base_sha, tuple(plan.reads))
        if not is_fresh(plan, model=model, prompt_version=PROMPT_VERSION, blobs=blobs):
            continue
        recipe = validate(plan.recipe)
        if isinstance(recipe, list):
            log.warning("저장한 처방 #%s이 지금의 처방 검사를 지나지 못함: %s", entry.id, recipe[0])
            continue
        return EnvReady(entry.id, recipe, plan.base_run, plan.reads, cached=True)
    return None


def _blobs(worktree: Path, base_sha: str, paths: Sequence[str]) -> dict[str, str]:
    """paths 중 base에 있는 것 → git 객체 해시(파일은 blob, 폴더는 tree)."""
    return {path: e.sha for path, e in repo.list_base_entries(worktree, base_sha, paths).items()}


def _root_listing(entries: Mapping[str, TreeEntry]) -> tuple[str, ...]:
    return tuple(sorted(p + "/" if e.mode == _TREE_MODE else p for p, e in entries.items()))


@dataclass(frozen=True)
class _Verified:
    """base에서 굽고 돌린 처방."""

    recipe: EnvRecipe
    run: TestRun
    reads: Mapping[str, str]


class _Stop(Exception):
    """회차를 더 돌지 않고 끝낸다 (호출 상한, 모델 호출 실패)."""

    def __init__(self, unplanned: EnvUnplanned) -> None:
        super().__init__(unplanned.reason)
        self.unplanned = unplanned


class _Planner:
    """처방 정하기 한 번. 회차 사이에 넘기는 것(직전 답, 고를 수 있는 처방)을 들고 있다."""

    def __init__(
        self, side: Path, base_sha: str, ask: AskModel, root: tuple[str, ...], platform: Platform,
        *, store: Store, budget: CallBudget, model: str, limits: Limits, names: docker_cli.Names,
    ) -> None:  # fmt: skip
        self.side, self.base_sha, self.ask = side, base_sha, ask
        self.root, self.platform = root, platform
        self.store, self.budget, self.model = store, budget, model
        self.limits, self.names = limits, names
        self.scope = EnvScope(budget.repo)
        self.answers: dict[str, tuple[Answer, Mapping[str, object]]] = {}
        self.rounds: list[Round] = []
        self.previous: Previous | None = None
        self.choosable: _Verified | None = None  # 직전 회차가 "모델이 고름"이면 그 처방
        self.ready: tuple[_Verified, str] | None = None  # 확인한 처방과 base가 빨갛다는 까닭

    def run(self) -> EnvPlan:
        policy = LoopPolicy(
            max_attempts=MAX_ROUNDS, may_call=self.budget.may_call(self.scope), heavy_after=None
        )
        try:
            final = build_loop("plan_env", self.write, self.verify, policy).invoke({"task": {}})
        except _Stop as stop:
            return replace(stop.unplanned, rounds=tuple(self.rounds))
        rounds = tuple(self.rounds)
        match final["outcome"]:
            case "accepted":
                assert self.ready is not None
                return self._save(*self.ready)
            case "dropped":
                return EnvUnplanned("nothing_to_run", final.get("detail", ""), rounds)
            case "budget":
                denied = self.budget.decide(self.scope, "light")
                why = denied.reason if isinstance(denied, Denied) else "호출 상한"
                return EnvUnplanned("cap", why, rounds)
            case _:
                last = rounds[-1].feedback if rounds else final.get("detail", "")
                return EnvUnplanned("unverified", last, rounds)

    def write(self, state: LoopState) -> WriteResult:
        number = state["attempt"]
        call = self.budget.begin(self.scope, stage="env", tier=state["tier"], model=self.model,
                                 attempt=number)  # fmt: skip
        if isinstance(call, Denied):
            raise _Stop(EnvUnplanned("cap", call.reason))
        result = self.ask(
            build_prompt(number, self.root, self.platform, self.previous),
            system_prompt=SYSTEM_PROMPT,
            schema=ANSWER_SCHEMA,
            model=self.model,
            max_budget_usd=CALL_BUDGET_USD,
            max_tool_uses=MAX_TOOL_USES,
        )
        log.info("환경 처방 %d회차 호출: 도구 %d회, 읽은 경로 %s, 입력 %s, 출력 %s토큰", number,
                 result.tool_uses, list(result.reads), result.input_tokens,
                 result.output_tokens)  # fmt: skip
        failure = None if result.ok else f"{result.error}: {result.detail}"
        answer, unreadable = None, ""
        if failure is None:
            try:
                answer = read_answer(result.data or {})
            except ValueError as e:
                unreadable = f"답의 형식이 틀림: {str(e).splitlines()[0]}"
        self.budget.finish(
            call, ok=answer is not None, error=failure or unreadable or None,
            model_id=result.model_id, input_tokens=result.input_tokens,
            output_tokens=result.output_tokens, permission_denials=result.permission_denials,
        )  # fmt: skip
        if failure is not None:
            raise _Stop(EnvUnplanned("model_failed", failure))
        if answer is None:
            self._note(number, unreadable, unreadable, answer=None, choosable=None)
            raise WriteFailed(unreadable)
        key = str(number)
        self.answers[key] = (answer, result.data or {})
        return WriteResult(artifact=key)

    def verify(self, state: LoopState) -> Verdict:
        number = state["attempt"]
        answer, raw = self.answers[state["artifact"]]
        match answer:
            case NothingToRun(reason=reason):
                self.rounds.append(Round(number, f"돌릴 것이 없다고 답함: {reason}"))
                return Verdict("drop", detail=reason)
            case BaseIsRed(reason=reason):
                if self.choosable is None:
                    return self._retry(number, raw, "고를 수 없는 회차에 base_is_red를 고름",
                                       RED_NOT_ALLOWED, choosable=None)  # fmt: skip
                self.ready = (self.choosable, reason)
                self.rounds.append(Round(number, f"base가 빨갛다고 답함: {reason}"))
                return Verdict("pass")
            case RecipeAnswer():
                return self._try_recipe(number, answer, raw)

    def _try_recipe(self, number: int, answer: RecipeAnswer, raw: Mapping[str, object]) -> Verdict:
        recipe = validate(answer.recipe)
        if isinstance(recipe, list):
            return self._retry(number, raw, f"처방 검사에서 고칠 곳 {len(recipe)}개",
                               problems_feedback(recipe), choosable=None)  # fmt: skip
        reads = plan_reads(answer, recipe)
        found = _blobs(self.side, self.base_sha, reads)
        problems = reads_problems(reads, found)
        if problems:
            return self._retry(number, raw, f"처방 검사에서 고칠 곳 {len(problems)}개",
                               problems_feedback(problems), choosable=None)  # fmt: skip
        repo.reset_worktree(self.side)
        layers = ensure_layers(recipe, self.side, self.limits, names=self.names)
        if isinstance(layers, BakeFailed):
            what = "시간 상한" if layers.timed_out else f"종료 코드 {layers.exit_code}"
            return self._retry(number, raw, f"층 {layers.layer}을 굽지 못함: {layers.step}, {what}",
                               bake_feedback(layers), choosable=None)  # fmt: skip
        with open_environment(layers, recipe, self.side, self.limits, names=self.names) as box:
            run = box.run()
        verified = _Verified(recipe, run, found)
        built = ", ".join(f"{layer.name} {'다시 씀' if layer.reused else '구움'}"
                          for layer in layers.layers)  # fmt: skip
        failing = failing_step(run) if not run.ok else None
        where = f" ({failing.name})" if failing is not None else ""
        summary = f"층 {built}, base 실행 {run.status}{where}"
        match judge_base_run(run, has_test_step=recipe.test is not None):
            case "proven":
                self.ready = (verified, "")
                self.rounds.append(Round(number, summary))
                return Verdict("pass")
            case "choose":
                return self._retry(number, raw, summary, run_feedback(run), choosable=verified)
            case "repair":
                return self._retry(number, raw, summary, run_feedback(run), choosable=None)

    def _retry(
        self, number: int, raw: Mapping[str, object], summary: str, feedback: str, *,
        choosable: _Verified | None,
    ) -> Verdict:  # fmt: skip
        self._note(number, summary, feedback, answer=raw, choosable=choosable)
        return Verdict("retry", detail=summary, feedback=(feedback,))

    def _note(
        self, number: int, summary: str, feedback: str, *, answer: Mapping[str, object] | None,
        choosable: _Verified | None,
    ) -> None:  # fmt: skip
        log.info("환경 처방 %d회차: %s", number, summary)
        self.rounds.append(Round(number, summary, feedback))
        self.previous = Previous(answer, feedback, may_choose_red=choosable is not None)
        self.choosable = choosable

    def _save(self, verified: _Verified, base_is_red: str) -> EnvReady:
        base_run = base_run_of(verified.run, base_is_red)
        plan_id = self.store.save_env_plan(EnvPlanEntry(
            repo=self.budget.repo, model=self.model, prompt_version=PROMPT_VERSION,
            recipe_json=json.dumps(recipe_json(verified.recipe), ensure_ascii=False),
            reads_json=json.dumps(dict(verified.reads), sort_keys=True),
            base_run_json=base_run_to_json(base_run),
        ))  # fmt: skip
        return EnvReady(plan_id, verified.recipe, base_run, verified.reads, cached=False,
                        rounds=tuple(self.rounds))  # fmt: skip
