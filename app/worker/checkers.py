"""검사기 작성 (docs/DESIGN.md 4.4, 5.3 (a)): 규칙마다 모델이 쓴 검사기를 예시로 확인해 채택한다.

규칙마다 공용 틀(worker/loop.py)을 한 번 돈다. write는 도구 없이 모델을 부르고 답(초안)을
`model_cache`에 저장한다. verify는 초안을 샌드박스에서 예시에 돌려 결정적으로 판정한다. 처음
두 회차는 ast-grep 규칙, 세 번째는 Python AST 스크립트다. ast-grep 회차에서 모델이 그 형식으로 가릴
수 없다고 답하면(`unfit`) 그 초안은 돌리지 않고 다음 회차부터 Python AST 스크립트로 쓴다. 모델은 세
회차 모두 `model.checker`다.

결과는 `checkers` 테이블에 규칙의 캐시 키별로 둔다. 채택한 검사기와 끝내 통과하지 못한 기록은
다시 묻지 않는다. 호출 상한, 모델 호출 실패, 도구 준비 실패로 끝난 규칙은 저장하지 않으므로
다음 검사가 이어 한다. 호출은 규칙 묶음 상한(9.2)에서 세고 재시도도 센다.
"""

import json
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from langgraph.graph.state import CompiledStateGraph

from app.models.cache_key import text_hash
from app.models.call_budget import CALL_BUDGET_USD, Denied, RulesetScope
from app.models.checker import Checker, CheckerKind, checker_id
from app.models.checker_authoring import (
    ENGINE_VERSION,
    MAX_ATTEMPTS,
    Checked,
    Draft,
    RuleCheck,
    Unchecked,
    answer_schema,
    build_prompt,
    checker_from_json,
    checker_key,
    checker_to_json,
    draft_from_json,
    draft_key,
    draft_to_json,
    judge_examples,
    kind_for_attempt,
    model_examples,
    parse_answer,
    plan_check,
    previous_body_feedback,
    prompt_version_for,
    shortfall,
    system_prompt,
)
from app.models.repo_config import ModelSettings
from app.models.rule import Rule, normalize
from app.sandbox import checker_runner, toolcache
from app.sandbox.checker_runner import CheckerRun, Failed, Invalid
from app.worker.ai import claude_cli
from app.worker.ai.claude_cli import StructuredResult
from app.worker.budget import CallBudget
from app.worker.loop import LoopPolicy, LoopState, Verdict, WriteFailed, WriteResult, build_loop
from app.worker.state import Store

log = logging.getLogger(__name__)

# (사용자 메시지, *, system_prompt, schema, model, max_budget_usd) → 결과. 테스트는 가짜를 넘긴다
AskModel = Callable[..., StructuredResult]


class RunsCheckers(Protocol):
    def on_examples(self, kind: CheckerKind, body: str, snippets: Sequence[str]) -> CheckerRun: ...

    def on_files(
        self, kind: CheckerKind, body: str, root: Path, files: Sequence[str]
    ) -> CheckerRun: ...


class Sandbox:
    """검사기를 샌드박스에서 돌린다. ast-grep은 처음 필요할 때 도구 캐시에 받는다 (2.3).

    받지 못하면 toolcache.ToolUnavailable(그 도구를 쓰는 규칙만 검사하지 못함) 또는
    SandboxError(환경 오류)가 올라간다.
    """

    def __init__(self, cache_dir: Path = toolcache.TOOL_CACHE) -> None:
        self.cache_dir = cache_dir
        self._ast_grep: Path | None = None

    def _tool_dir(self, kind: CheckerKind) -> Path | None:
        if kind != "ast-grep":
            return None
        if self._ast_grep is None:
            self._ast_grep = toolcache.ensure("ast-grep", cache_dir=self.cache_dir)
        return self._ast_grep

    def on_examples(self, kind: CheckerKind, body: str, snippets: Sequence[str]) -> CheckerRun:
        return checker_runner.run_on_examples(kind, body, snippets, tool_dir=self._tool_dir(kind))

    def on_files(
        self, kind: CheckerKind, body: str, root: Path, files: Sequence[str]
    ) -> CheckerRun:
        return checker_runner.run_checker(kind, body, root, files, tool_dir=self._tool_dir(kind))


class _CapHit(Exception):
    def __init__(self, denied: Denied) -> None:
        super().__init__(denied.reason)
        self.denied = denied


class _ModelCallFailed(Exception):
    pass


def plan_and_author(
    rules: Sequence[Rule],
    *,
    ruleset_key: str,
    store: Store,
    budget: CallBudget,
    models: ModelSettings,
    ask: AskModel = claude_cli.run_structured,
    sandbox: RunsCheckers | None = None,
) -> tuple[RuleCheck, ...]:
    """규칙마다 검사 방법을 정하고, 검사기가 필요한 규칙은 검사기를 작성해 예시로 확인한다.

    ruleset_key는 규칙 읽기의 캐시 키(`RulesFound.key`)다. 규칙 묶음 상한이 이 키로 센다.
    """
    sandbox = sandbox or Sandbox()
    scope = RulesetScope(ruleset_key, len(rules))
    author = _Author(store=store, budget=budget, scope=scope, models=models, ask=ask,
                     sandbox=sandbox)  # fmt: skip
    return tuple(author.check(rule) for rule in rules)


class _Author:
    def __init__(
        self,
        *,
        store: Store,
        budget: CallBudget,
        scope: RulesetScope,
        models: ModelSettings,
        ask: AskModel,
        sandbox: RunsCheckers,
    ) -> None:
        self.store, self.budget, self.scope = store, budget, scope
        self.models, self.ask, self.sandbox = models, ask, sandbox

    def check(self, rule: Rule) -> RuleCheck:
        skipped = plan_check(rule)
        if skipped is not None:
            return skipped
        key = checker_key(rule, self.models.checker)
        saved = self.store.saved_checker(key)
        if saved is not None:
            self.budget.record_hit(self.scope, stage="checker", tier="light",
                                   model=self.models.checker)  # fmt: skip
            if saved.accepted and saved.checker_json is not None:
                return Checked(rule, checker_from_json(saved.checker_json), cached=True)
            return Unchecked(rule, "no_checker", saved.detail)
        try:
            final = self._loop(rule, key).invoke({"task": {"rule_id": rule.id}})
        except _CapHit as e:
            return Unchecked(rule, "cap", e.denied.reason)
        except _ModelCallFailed as e:
            return Unchecked(rule, "model_failed", str(e))
        except toolcache.ToolUnavailable as e:
            return Unchecked(rule, "tool_unavailable", f"도구를 준비하지 못함: {e}")
        outcome = final["outcome"]
        if outcome == "budget":
            denied = self.budget.decide(self.scope, "light")
            reason = denied.reason if isinstance(denied, Denied) else "호출 상한"
            return Unchecked(rule, "cap", reason)
        attempts = json.dumps(final.get("history", []), ensure_ascii=False)
        if outcome == "accepted":
            checker = self._accepted(rule, final["artifact"])
            self.store.save_checker(key, rule_id=rule.id, accepted=True,
                                    checker_json=checker_to_json(checker), detail="",
                                    attempts_json=attempts)  # fmt: skip
            return Checked(rule, checker, cached=False)
        detail = f"{MAX_ATTEMPTS}회 안에 예시를 통과하지 못함: {final.get('detail', '')}"
        log.info("검사기 작성 실패 (%s): %s", rule.id, detail)
        self.store.save_checker(key, rule_id=rule.id, accepted=False, checker_json=None,
                                detail=detail, attempts_json=attempts)  # fmt: skip
        return Unchecked(rule, "no_checker", detail)

    def _draft(self, artifact: str) -> Draft:
        entry = self.store.cached_result(artifact)
        if entry is None:
            raise LookupError(f"저장한 검사기 초안이 없다: {artifact}")
        return draft_from_json(entry.result_json)

    def _accepted(self, rule: Rule, artifact: str) -> Checker:
        draft = self._draft(artifact)
        return Checker(
            id=checker_id(draft.body), rule_id=rule.id,
            rule_text_hash=text_hash(normalize(rule.text)), kind=draft.kind, body=draft.body,
            examples=(*rule.examples, *draft.examples),
            engine_version=ENGINE_VERSION[draft.kind], model=draft.model,
            verified_at=datetime.now(UTC),
        )  # fmt: skip

    def _loop(self, rule: Rule, key: str) -> CompiledStateGraph:
        need = shortfall(rule.examples)

        def write(state: LoopState) -> WriteResult:
            attempt, tier = state["attempt"], state["tier"]
            feedback = state.get("feedback", [])
            model = self.models.checker  # 더 높은 모델로 올리지 않는다 (11.2 D7)
            unfit = any(self._draft(a["artifact"]).unfit
                        for a in state.get("history", []) if a["artifact"])  # fmt: skip
            kind = kind_for_attempt(attempt, rule, ast_grep_unfit=unfit)
            artifact = draft_key(key, attempt, model, feedback, prompt_version_for(rule))
            if self.store.cached_result(artifact) is not None:
                self.budget.record_hit(self.scope, stage="checker", tier=tier, model=model)
                return WriteResult(artifact=artifact)
            call = self.budget.begin(self.scope, stage="checker", tier=tier, model=model,
                                     attempt=attempt)  # fmt: skip
            if isinstance(call, Denied):
                raise _CapHit(call)
            answer = self.ask(
                build_prompt(rule, need, feedback),
                system_prompt=system_prompt(kind),
                schema=answer_schema(kind),
                model=model,
                max_budget_usd=CALL_BUDGET_USD[tier],
            )
            failure = None if answer.ok else f"모델 호출 실패: {answer.error}: {answer.detail}"
            parsed = None
            if failure is None:
                try:
                    parsed = parse_answer(answer.data or {})
                except ValueError as e:
                    failure = f"답의 형식이 틀림: {str(e).splitlines()[0]}"
            self.budget.finish(
                call, ok=parsed is not None, error=failure, model_id=answer.model_id,
                input_tokens=answer.input_tokens, output_tokens=answer.output_tokens,
                permission_denials=answer.permission_denials,
            )  # fmt: skip
            if not answer.ok:
                raise _ModelCallFailed(failure)
            if parsed is None:
                raise WriteFailed(failure)
            examples, problems = model_examples(rule.examples, parsed, kind)
            draft = Draft(
                kind=kind, body=parsed.checker, examples=examples,
                example_problems=tuple(problems), model=answer.model_id or model,
                unfit=parsed.unfit.strip() if kind == "ast-grep" else "",
            )  # fmt: skip
            self.store.save_result(artifact, stage="checker", model=draft.model,
                                   prompt_version=prompt_version_for(rule),
                                   result_json=draft_to_json(draft))  # fmt: skip
            return WriteResult(artifact=artifact)

        def verify(state: LoopState) -> Verdict:
            draft = self._draft(state["artifact"])
            if draft.unfit:
                said = f"직전 시도는 이 규칙을 ast-grep으로 가릴 수 없다고 답했다: {draft.unfit}"
                return Verdict(
                    "retry", detail=f"ast-grep으로 가릴 수 없다고 답함: {draft.unfit}",
                    feedback=(f"{said}\n이번에는 Python ast 스크립트로 쓴다.",),
                )  # fmt: skip
            previous = previous_body_feedback(draft)
            if draft.example_problems:
                return Verdict("retry", detail=draft.example_problems[0],
                               feedback=(*draft.example_problems, previous))  # fmt: skip
            examples = (*rule.examples, *draft.examples)
            run = self.sandbox.on_examples(draft.kind, draft.body, [e.code for e in examples])
            if isinstance(run, Invalid | Failed):
                what = "검사기가 틀림" if isinstance(run, Invalid) else "검사기 실행이 실패함"
                first = run.reason.strip().splitlines()[0] if run.reason.strip() else ""
                return Verdict("retry", detail=f"{what}: {first}",
                               feedback=(f"{what}:\n{run.reason}", previous))  # fmt: skip
            spans = [
                [(m.line, m.end_line) for m in run.matches
                 if m.file == checker_runner.example_file(draft.kind, i, e.code)]
                for i, e in enumerate(examples)
            ]  # fmt: skip
            problems = judge_examples(examples, spans)
            if not problems:
                return Verdict("pass")
            return Verdict("retry", detail=f"예시 {len(problems)}개를 틀리게 가림",
                           feedback=(*problems, previous))  # fmt: skip

        policy = LoopPolicy(max_attempts=MAX_ATTEMPTS, may_call=self.budget.may_call(self.scope),
                            heavy_after=None)  # fmt: skip
        return build_loop(f"author_checker[{rule.id}]", write, verify, policy)
