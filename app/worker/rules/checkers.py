"""검사 방법 정하기 (docs/DESIGN.md 4.4, 5.3 (a)): 규칙마다 저장소의 도구, 모델이 쓴 검사기 순서로
확인 방법을 정하고 예시로 확인해 채택한다.

도구 설정에서 온 규칙 묶음(ruff 설정)은 모델 없이 ruff로 정한다. 문서 규칙은 먼저 모델에게 도구
목록(ruff, ruff-format)에서 맞는 도구를 묻는다(`_choose_tool`). 고른 도구는 검사기와 같은 예시
확인을 통과해야 채택하고, 모델이 고른 ruff 코드는 `ruff rule`로 실재하는지 본다. 맞는 도구가 없거나
확인을 통과하지 못하면 검사기를 작성한다. 도구 선택의 결과도 `checkers` 테이블에 그 키로 둔다.
저장소가 고정한 ruff 버전을 파이프라인이 받을 수 없으면 도구 목록을 내밀지 않는다.

검사기 작성은 규칙마다 공용 틀(app/worker/loop.py)을 한 번 돈다. write는 도구 없이 모델을 부르고
답(초안)을 `model_cache`에 저장한다. verify는 초안을 샌드박스에서 예시에 돌려 결정적으로
판정한다. 처음 두 회차는 ast-grep 규칙, 세 번째는 Python AST 스크립트다. ast-grep 회차에서
모델이 그 형식으로 가릴 수 없다고 답하면(`unfit`) 그 초안은 돌리지 않고 다음 회차부터 Python AST
스크립트로 쓴다. 모델은 세 회차 모두 `model.checker`다.

내용을 보는 규칙은 아래 때에 검사기를 채택하지 않고 모델 판단(`ToJudge`, 4.5)으로 보낸다 (4.4
"의미 규칙을 모델 판단으로 보내는 때"): 모델이 검사기로 일부만·전혀 가릴 수 없다고 답한 회차(그
초안은 돌리지 않는다), 예시 확인의 두 번째 실패, 세 회차를 다 쓴 때. 경로 규칙은 모델 판단으로
보내지 않고 세 회차 뒤 "검사하지 못한 규칙"으로 남는다.

결과는 `checkers` 테이블에 규칙의 캐시 키별로 둔다(모델 판단으로 보낸 까닭은 `judge_route`).
채택한 검사기, 모델 판단으로 보낸 기록, 끝내 통과하지 못한 기록은 다시 묻지 않는다. 호출 상한, 모델
호출 실패, 도구 준비 실패로 끝난 규칙은 저장하지 않으므로 다음 검사가 이어 한다. 호출은 규칙 묶음
상한(9.2)에서 세고 재시도도 센다.
"""

import json
import logging
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Protocol

from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.exc import SQLAlchemyError

from app.worker.ai.model_backend import AskModel
from app.worker.checker_authoring_pure import (
    ENGINE_VERSION,
    MAX_ATTEMPTS,
    RECORDS_NOTE,
    Answer,
    Checked,
    Draft,
    JudgeRoute,
    RuleCheck,
    ToJudge,
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
    judge_route,
    judged_after_failures,
    kind_for_attempt,
    model_examples,
    parse_answer,
    plan_check,
    previous_body_feedback,
    prompt_version_for,
    route_before_run,
    shortfall,
    system_prompt,
)
from app.worker.config.repo_config_pure import ModelSettings
from app.worker.core.cache_key_pure import text_hash
from app.worker.core.checker_pure import TOOL_KINDS, Checker, CheckerKind, checker_id, tool_codes
from app.worker.core.rule_pure import Rule, looks_at_paths, normalize
from app.worker.core.tools_pure import RUFF, RuffSetup, valid_codes
from app.worker.lang import python
from app.worker.loop import LoopPolicy, LoopState, Verdict, WriteFailed, WriteResult, build_loop
from app.worker.rules import tool_choice_pure
from app.worker.sandbox import checker_runner, docker_cli, toolcache
from app.worker.sandbox.checker_runner import CheckerRun, Failed, Invalid
from app.worker.store import records, records_pure
from app.worker.store.budget import CallBudget
from app.worker.store.call_budget_pure import CALL_BUDGET_USD, Denied, RulesetScope
from app.worker.store.memory import FindsRecords
from app.worker.store.rag_pure import (
    EMPTY,
    Retrieval,
    Selection,
    query_text,
    render_records,
    retrieval,
)
from app.worker.store.state import Store

log = logging.getLogger(__name__)

STALE: Final = "새 커밋이 와서 멈춤"
# 검사기 종류마다 도구 캐시에서 붙이는 도구. 이미지에는 언어가 없어 Python AST 스크립트와 경로
# 패턴의 고정 스크립트도 Python 런타임을 받아 붙인다
KIND_TOOL: Final[Mapping[CheckerKind, str]] = {
    "ast-grep": "ast-grep",
    "python-ast": toolcache.PYTHON,
    "path-pattern": toolcache.PYTHON,
    "ruff": RUFF,
    "ruff-format": RUFF,
}


class RunsCheckers(Protocol):
    def on_examples(self, kind: CheckerKind, body: str, snippets: Sequence[str]) -> CheckerRun: ...

    def on_files(
        self, kind: CheckerKind, body: str, root: Path, files: Sequence[str]
    ) -> CheckerRun: ...

    def rule_exists(self, code: str) -> bool: ...


class FormatsFiles(RunsCheckers, Protocol):
    """기계적 수정이 쓰는 샌드박스: 도구의 검사에 더해 `ruff format`."""

    def format_check(self, root: Path) -> python.FormatResult: ...

    def format_apply(self, root: Path, files: Sequence[str]) -> python.FormatResult: ...


class Sandbox:
    """검사기와 도구를 샌드박스에서 돌린다. 도구는 처음 필요할 때 도구 캐시에 받는다 (2.3).

    이미지에는 언어가 없어서 검사기 종류마다 도구 캐시의 도구 하나를 붙인다(`KIND_TOOL`). 이미지는
    처음 컨테이너가 필요할 때 한 번 빌드한다. ruff는 이 저장소의 버전(저장소가 고정한 것 먼저)과
    base의 설정으로 돈다. 받지 못하면 toolcache.ToolUnavailable(그 도구를 쓰는 규칙만 검사하지 못함)
    또는 SandboxError(환경 오류)가 올라간다.
    """

    def __init__(
        self,
        cache_dir: Path = toolcache.TOOL_CACHE,
        ruff: RuffSetup | None = None,
        image: str = docker_cli.IMAGE,
    ) -> None:
        self.cache_dir = cache_dir
        self.ruff = ruff or RuffSetup(toolcache.TOOLS[RUFF].default)
        self.image = image
        self._built = False
        self._dirs: dict[str, Path] = {}

    def _tool_dir(self, kind: CheckerKind) -> Path:
        if not self._built:
            docker_cli.ensure_image(self.image)
            self._built = True
        name = KIND_TOOL[kind]
        if name not in self._dirs:
            version = self.ruff.version if name == RUFF else None
            self._dirs[name] = toolcache.ensure(
                name, version, cache_dir=self.cache_dir, download=toolcache.fetcher(self.image)
            )
        return self._dirs[name]

    def on_examples(self, kind: CheckerKind, body: str, snippets: Sequence[str]) -> CheckerRun:
        if kind not in TOOL_KINDS:
            return checker_runner.run_on_examples(
                kind, body, snippets, tool_dir=self._tool_dir(kind), image=self.image
            )
        with tempfile.TemporaryDirectory(prefix="pipeline-examples-") as tmp:
            root = Path(tmp)
            (root / checker_runner.EXAMPLE_DIR).mkdir()
            for i, code in enumerate(snippets):
                (root / checker_runner.example_path(i)).write_text(code)
            files = [checker_runner.example_path(i) for i in range(len(snippets))]
            # 예시는 저장소 밖의 코드라 base 설정(경로별 끄기, exclude)을 적용하지 않는다
            return self._tool(kind, body, root, files, isolated=True)

    def on_files(
        self, kind: CheckerKind, body: str, root: Path, files: Sequence[str]
    ) -> CheckerRun:
        if kind in TOOL_KINDS:
            return self._tool(kind, body, root, files, isolated=False)
        return checker_runner.run_checker(
            kind, body, root, files, tool_dir=self._tool_dir(kind), image=self.image
        )

    def _tool(
        self, kind: CheckerKind, body: str, root: Path, files: Sequence[str], *, isolated: bool
    ) -> CheckerRun:
        tool_dir = self._tool_dir(kind)
        config = None if isolated else self.ruff.config
        if kind == "ruff-format":
            return python.format_diff(
                root, files, tool_dir=tool_dir, image=self.image, config=config
            )
        return python.ruff_check(
            root, files, tool_dir=tool_dir, image=self.image, config=config,
            codes=tool_codes(body),
        )  # fmt: skip

    def rule_exists(self, code: str) -> bool:
        return python.ruff_rule_exists(code, tool_dir=self._tool_dir("ruff"), image=self.image)

    def pr_pattern(
        self, decl: Mapping[str, object], cases: Sequence[Mapping[str, object]]
    ) -> checker_runner.PrRan | Failed:
        """PR 패턴 선언을 사례들에 맞춘다 (4.5 끝). 경로 패턴과 같은 Python 런타임을 붙인다."""
        return checker_runner.run_pr_pattern(
            decl, cases, tool_dir=self._tool_dir("path-pattern"), image=self.image
        )

    def format_check(self, root: Path) -> python.FormatResult:
        """root 전체가 base 설정의 `ruff format`을 따르는지 (4.7)."""
        return python.format_check(
            root, tool_dir=self._tool_dir("ruff-format"), image=self.image, config=self.ruff.config
        )

    def format_apply(self, root: Path, files: Sequence[str]) -> python.FormatResult:
        return python.format_apply(
            root, files, tool_dir=self._tool_dir("ruff-format"), image=self.image,
            config=self.ruff.config,
        )  # fmt: skip


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
    ask: AskModel,
    sandbox: RunsCheckers | None = None,
    ruff: RuffSetup | None = None,
    is_stale: Callable[[], bool] = lambda: False,
    retriever: FindsRecords | None = None,
    on_retrieval: Callable[[Retrieval], None] = lambda r: None,
    secrets: Sequence[str] = (),
) -> tuple[RuleCheck, ...]:
    """규칙마다 검사 방법을 정하고, 도구나 검사기가 필요한 규칙은 예시로 확인한다.

    ruleset_key는 규칙 읽기의 캐시 키(`RulesFound.key`)다. 규칙 묶음 상한이 이 키로 센다.
    ruff는 이 저장소의 ruff다. 없거나 받을 수 없는 버전이면 문서 규칙에 도구 목록을 내밀지 않는다.
    새 커밋이 와서 낡았으면(is_stale) 남은 규칙은 작성하지 않는다. 저장하지 않아 다음 검사가 한다.

    채택한 검사기는 검사기 기록(`records`, 4.10)으로 남긴다(저장 전에 secrets와 토큰 형식을 본다).
    retriever가 있으면(검색을 켬) 검사기를 쓸 때 비슷한 규칙의 검사기 기록을 참고 자료로 넣고, 넣은
    것을 on_retrieval로 알린다 (6.2).
    """
    sandbox = sandbox or Sandbox()
    scope = RulesetScope(ruleset_key, len(rules))
    author = _Author(store=store, budget=budget, scope=scope, models=models, ask=ask,
                     sandbox=sandbox, ruff=ruff, retriever=retriever, on_retrieval=on_retrieval,
                     secrets=secrets)  # fmt: skip
    out: list[RuleCheck] = []
    for rule in rules:
        if is_stale():
            out.append(Unchecked(rule, "model_failed", STALE))
            continue
        out.append(author.check(rule))
    return tuple(out)


def config_checker(rule: Rule, ruff: RuffSetup) -> Checker:
    """도구 설정에서 온 규칙 묶음의 확인 방법: base 설정의 규칙 전부로 ruff를 돌린다 (4.3)."""
    body = json.dumps({"codes": []})
    return Checker(
        id=checker_id(body), rule_id=rule.id, rule_text_hash=text_hash(normalize(rule.text)),
        kind="ruff", body=body, examples=(), engine_version=f"ruff-{ruff.version}",
    )  # fmt: skip


def config_key(rule: Rule, ruff: RuffSetup) -> str:
    """도구 설정 규칙의 `checkers` 키. 설정 파일의 blob과 ruff 버전으로 정해진다."""
    return f"tool-config:{rule.source.file}:{rule.source.blob_sha}:ruff-{ruff.version}"


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
        ruff: RuffSetup | None = None,
        retriever: FindsRecords | None = None,
        on_retrieval: Callable[[Retrieval], None] = lambda r: None,
        secrets: Sequence[str] = (),
    ) -> None:
        self.store, self.budget, self.scope = store, budget, scope
        self.models, self.ask, self.sandbox = models, ask, sandbox
        self.ruff = ruff
        self.retriever, self.on_retrieval, self.secrets = retriever, on_retrieval, secrets

    def _records(self, rule: Rule) -> Selection:
        """비슷한 규칙의 검사기 기록 (6.2). 검색을 켜지 않았으면 빈 선택."""
        if self.retriever is None:
            return EMPTY
        selection = self.retriever.find("checker", [query_text(rule.text, kind="checker")])
        self.on_retrieval(replace(retrieval(selection, "checker"), error=self.retriever.error))
        return selection

    def _remember(self, rule: Rule, checker: Checker) -> None:
        """채택한 검사기를 검사기 기록으로 남긴다 (4.10 표의 마지막 줄)."""
        new = records_pure.checker_record(
            repo=self.budget.repo, rule_id=rule.id, rule_text=rule.text, checker_id=checker.id,
            kind=checker.kind, body=checker.body,
            examples=[(e.code, e.expect_lines) for e in checker.examples],
        )  # fmt: skip
        try:
            records.save(self.store, [new], self.secrets)
        except SQLAlchemyError:
            log.exception("검사기 기록을 남기지 못함 %s", rule.id)

    def check(self, rule: Rule) -> RuleCheck:
        skipped = plan_check(rule)
        if skipped is not None:
            return skipped
        if rule.origin == "tool_config":
            return self._config(rule)
        if self.ruff is not None and self.ruff.available and not looks_at_paths(rule):
            chosen = self._choose_tool(rule)
            if chosen is not None:
                return chosen
        key = checker_key(rule, self.models.checker)
        saved = self.store.saved_checker(key)
        if saved is not None:
            self.budget.record_hit(self.scope, stage="checker", tier="light",
                                   model=self.models.checker)  # fmt: skip
            if saved.accepted and saved.checker_json is not None:
                return Checked(rule, checker_from_json(saved.checker_json), cached=True)
            route = judge_route(saved.judge_route)
            if route is not None:
                return ToJudge(rule, route, saved.detail)
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
            self._remember(rule, checker)
            return Checked(rule, checker, cached=False)
        route: JudgeRoute | None
        if outcome == "dropped":
            # verify가 모델 판단으로 보냈다: 모델이 검사기로 다 가릴 수 없다고 답했거나, 예시 확인의
            # 두 번째 실패다
            last = self._draft(final["history"][-1]["artifact"])
            route = route_before_run(last) or "examples"
            detail = final.get("detail", "")
        elif looks_at_paths(rule):
            route = None
            detail = f"{MAX_ATTEMPTS}회 안에 예시를 통과하지 못함: {final.get('detail', '')}"
        else:
            route = "attempts"
            detail = f"{MAX_ATTEMPTS}회 안에 예시를 통과하지 못함: {final.get('detail', '')}"
        log.info("검사기를 채택하지 않음 (%s, %s): %s", rule.id, route or "경로 규칙", detail)
        self.store.save_checker(key, rule_id=rule.id, accepted=False, checker_json=None,
                                detail=detail, attempts_json=attempts,
                                judge_route=route)  # fmt: skip
        if route is None:
            return Unchecked(rule, "no_checker", detail)
        return ToJudge(rule, route, detail)

    def _config(self, rule: Rule) -> RuleCheck:
        """도구 설정의 규칙 묶음. 모델을 부르지 않는다. 받을 수 없는 ruff 버전이면 검사하지
        못한다."""
        if self.ruff is None or not self.ruff.available:
            label = self.ruff.label if self.ruff is not None else "ruff"
            detail = f"도구를 준비하지 못함: {label}은 파이프라인이 해시를 고정한 버전이 아님"
            return Unchecked(rule, "tool_unavailable", detail)
        checker = config_checker(rule, self.ruff)
        self.store.save_checker(config_key(rule, self.ruff), rule_id=rule.id, accepted=True,
                                checker_json=checker_to_json(checker), detail="",
                                attempts_json="[]")  # fmt: skip
        return Checked(rule, checker, cached=True)

    def _choose_tool(self, rule: Rule) -> RuleCheck | None:
        """도구 목록에서 이 규칙을 확인할 도구를 고른다 (4.4 (a)). 채택하면 Checked, 호출 상한·모델
        실패·도구 준비 실패면 Unchecked(저장하지 않아 다음 검사가 다시 묻는다), 맞는 도구가 없거나
        예시 확인을 통과하지 못하면 None(검사기 작성으로 넘어간다)."""
        assert self.ruff is not None
        model = self.models.checker
        key = tool_choice_pure.choice_key(rule, model, self.ruff.version)
        saved = self.store.saved_checker(key)
        if saved is not None:
            self.budget.record_hit(self.scope, stage="checker", tier="light", model=model)
            if saved.accepted and saved.checker_json is not None:
                return Checked(rule, checker_from_json(saved.checker_json), cached=True)
            return None
        call = self.budget.begin(self.scope, stage="checker", tier="light", model=model)
        if isinstance(call, Denied):
            return Unchecked(rule, "cap", call.reason)
        answer = self.ask(
            tool_choice_pure.build_prompt(rule, shortfall(rule.examples)),
            system_prompt=tool_choice_pure.SYSTEM_PROMPT,
            schema=tool_choice_pure.ANSWER_SCHEMA,
            model=model,
            max_budget_usd=CALL_BUDGET_USD["light"],
        )
        failure = None if answer.ok else f"모델 호출 실패: {answer.error}: {answer.detail}"
        parsed = None
        if failure is None:
            try:
                parsed = tool_choice_pure.parse_answer(answer.data or {})
            except ValueError as e:
                failure = f"도구 선택의 답 형식이 틀림: {str(e).splitlines()[0]}"
        self.budget.finish(
            call, ok=parsed is not None, error=failure, model_id=answer.model_id,
            input_tokens=answer.input_tokens, output_tokens=answer.output_tokens,
            permission_denials=answer.permission_denials,
        )  # fmt: skip
        if not answer.ok:
            return Unchecked(rule, "model_failed", failure or "")
        try:
            verdict = (
                self._verify_choice(rule, parsed, answer.model_id or model)
                if parsed is not None else failure or ""
            )  # fmt: skip
        except toolcache.ToolUnavailable as e:
            return Unchecked(rule, "tool_unavailable", f"도구를 준비하지 못함: {e}")
        said = parsed.model_dump() if parsed is not None else {}
        attempts = json.dumps([{"answer": said}], ensure_ascii=False)
        if isinstance(verdict, Checker):
            self.store.save_checker(key, rule_id=rule.id, accepted=True,
                                    checker_json=checker_to_json(verdict), detail="",
                                    attempts_json=attempts)  # fmt: skip
            return Checked(rule, verdict, cached=False)
        log.info("도구로 확인하지 않음 (%s): %s", rule.id, verdict)
        self.store.save_checker(key, rule_id=rule.id, accepted=False, checker_json=None,
                                detail=verdict, attempts_json=attempts)  # fmt: skip
        return None

    def _verify_choice(
        self, rule: Rule, answer: tool_choice_pure.Answer, model: str
    ) -> Checker | str:
        """고른 도구를 결정적으로 확인한다. 채택하면 검사기, 아니면 그 이유."""
        assert self.ruff is not None
        if answer.tool == tool_choice_pure.NO_TOOL:
            return f"맞는 도구가 없음 ({answer.reason})" if answer.reason else "맞는 도구가 없음"
        kind: CheckerKind = "ruff" if answer.tool == RUFF else "ruff-format"
        codes: tuple[str, ...] = ()
        if kind == "ruff":
            checked = valid_codes(answer.codes)
            if checked is None:
                return f"ruff 규칙 코드의 형식이 틀림: {', '.join(answer.codes) or '없음'}"
            missing = [c for c in checked if not self.sandbox.rule_exists(c)]
            if missing:
                return f"ruff {self.ruff.version}에 없는 규칙 코드: {', '.join(missing)}"
            codes = checked
        raw = Answer(checker="", examples=[e.model_dump() for e in answer.examples])
        examples, problems = model_examples(rule.examples, raw)
        if problems:
            return f"모델이 쓴 예시를 쓸 수 없음: {problems[0]}"
        body = json.dumps({"codes": list(codes)}) if kind == "ruff" else "{}"
        cases = (*rule.examples, *examples)
        run = self.sandbox.on_examples(kind, body, [e.code for e in cases])
        if isinstance(run, Invalid | Failed):
            first = run.reason.strip().splitlines()[0] if run.reason.strip() else ""
            return f"도구 실행이 실패함: {first}"
        spans = [
            [(m.line, m.end_line) for m in run.matches
             if m.file == checker_runner.example_file(kind, i, e.code)]
            for i, e in enumerate(cases)
        ]  # fmt: skip
        wrong = judge_examples(cases, spans)
        if wrong:
            return f"예시 {len(wrong)}개를 틀리게 가림: {wrong[0].splitlines()[0]}"
        return Checker(
            id=checker_id(body), rule_id=rule.id, rule_text_hash=text_hash(normalize(rule.text)),
            kind=kind, body=body, examples=cases, engine_version=f"ruff-{self.ruff.version}",
            model=model, verified_at=datetime.now(UTC),
        )  # fmt: skip

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
        found: list[Selection] = []  # 검색은 규칙마다 한 번 (회차마다 같은 기록을 넣는다)

        def records() -> Selection:
            if not found:
                found.append(self._records(rule))
            return found[0]

        def write(state: LoopState) -> WriteResult:
            attempt, tier = state["attempt"], state["tier"]
            feedback = state.get("feedback", [])
            model = self.models.checker  # 더 높은 모델로 올리지 않는다 (11.2 D7)
            unfit = any(self._draft(a["artifact"]).unfit
                        for a in state.get("history", []) if a["artifact"])  # fmt: skip
            kind = kind_for_attempt(attempt, rule, ast_grep_unfit=unfit)
            chosen = records()
            shown = render_records(chosen.chosen)
            artifact = draft_key(key, attempt, model, feedback, prompt_version_for(rule),
                                 records=chosen.ids)  # fmt: skip
            if self.store.cached_result(artifact) is not None:
                self.budget.record_hit(self.scope, stage="checker", tier=tier, model=model)
                return WriteResult(artifact=artifact)
            call = self.budget.begin(self.scope, stage="checker", tier=tier, model=model,
                                     attempt=attempt)  # fmt: skip
            if isinstance(call, Denied):
                raise _CapHit(call)
            answer = self.ask(
                build_prompt(rule, need, feedback, shown),
                system_prompt=system_prompt(kind) + (RECORDS_NOTE if shown else ""),
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
                coverage=parsed.coverage if kind != "path-pattern" else "all",
                uncovered=parsed.uncovered.strip() if kind != "path-pattern" else "",
            )  # fmt: skip
            self.store.save_result(artifact, stage="checker", model=draft.model,
                                   prompt_version=prompt_version_for(rule),
                                   result_json=draft_to_json(draft))  # fmt: skip
            return WriteResult(artifact=artifact)

        def failed(state: LoopState, detail: str, feedback: tuple[str, ...]) -> Verdict:
            """예시 확인을 통과하지 못한 회차. 두 번째면 더 돌리지 않는다 (경로 규칙 빼고)."""
            failures = 1 + sum(
                1 for a in state.get("history", [])
                if a["result"] == "rejected" and not self._draft(a["artifact"]).unfit
            )  # fmt: skip
            if judged_after_failures(rule, failures):
                return Verdict("drop", detail=f"예시 확인을 {failures}번 통과하지 못함: {detail}")
            return Verdict("retry", detail=detail, feedback=feedback)

        def verify(state: LoopState) -> Verdict:
            draft = self._draft(state["artifact"])
            if route_before_run(draft) is not None:
                return Verdict("drop", detail=draft.uncovered)
            if draft.unfit:
                said = f"직전 시도는 이 규칙을 ast-grep으로 가릴 수 없다고 답했다: {draft.unfit}"
                return Verdict(
                    "retry", detail=f"ast-grep으로 가릴 수 없다고 답함: {draft.unfit}",
                    feedback=(f"{said}\n이번에는 Python ast 스크립트로 쓴다.",),
                )  # fmt: skip
            previous = previous_body_feedback(draft)
            if draft.example_problems:
                return failed(state, draft.example_problems[0],
                              (*draft.example_problems, previous))  # fmt: skip
            examples = (*rule.examples, *draft.examples)
            run = self.sandbox.on_examples(draft.kind, draft.body, [e.code for e in examples])
            if isinstance(run, Invalid | Failed):
                what = "검사기가 틀림" if isinstance(run, Invalid) else "검사기 실행이 실패함"
                first = run.reason.strip().splitlines()[0] if run.reason.strip() else ""
                return failed(state, f"{what}: {first}", (f"{what}:\n{run.reason}", previous))
            spans = [
                [(m.line, m.end_line) for m in run.matches
                 if m.file == checker_runner.example_file(draft.kind, i, e.code)]
                for i, e in enumerate(examples)
            ]  # fmt: skip
            problems = judge_examples(examples, spans)
            if not problems:
                return Verdict("pass")
            return failed(state, f"예시 {len(problems)}개를 틀리게 가림", (*problems, previous))

        policy = LoopPolicy(max_attempts=MAX_ATTEMPTS, may_call=self.budget.may_call(self.scope),
                            heavy_after=None)  # fmt: skip
        return build_loop(f"author_checker[{rule.id}]", write, verify, policy)
