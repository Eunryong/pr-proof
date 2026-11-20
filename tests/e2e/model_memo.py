"""실제 모델로 도는 평가 세트의 호출 기록과 같은 입력의 답 다시 쓰기 (DESIGN.md 13절 6-1, 6-3).

케이스마다 빈 데이터 폴더라, 같은 README를 쓰는 케이스는 규칙 읽기·도구 선택·검사기 작성을 케이스
수만큼 다시 묻는다. 케이스 파일 전부를 실제 모델로 돌리면 한 번에 550회쯤이다. 운영은 같은 입력의
답을 `model_cache`에 저장해 다시 쓰므로(2.1), 평가 실행 하나 안에서도 도구 없는 호출의 답을 입력
(프롬프트, 시스템 프롬프트, 스키마, 모델)마다 한 번만 묻는다. 수정 작성(파일 도구)은 다시 쓰지
않는다.

`PR_PROOF_MODEL_MEMO`가 가리키는 파일에 한 줄씩 남긴다: 실제로 부른 도구 없는 호출(`call`, 답 포함),
저장한 답을 다시 쓴 것(`hit`), 수정 작성(`fix`). 같은 파일로 다시 돌리면 그 파일의 답을 다시 쓴다.
"""

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Final, Literal

from app.models import (
    checker_authoring,
    fix_decision,
    judge,
    pr_check,
    rule_reading,
    tool_choice,
)
from app.worker.ai.claude_cli import AIErrorKind, ClaudeResult, StructuredResult

Kind = Literal["call", "hit", "fix"]

# 스키마 → 단계 이름. 호출 기록을 단계별로 세고, 한 단계의 답만 지우고 다시 묻는 데 쓴다
STAGES: Final[tuple[tuple[Mapping[str, object], str], ...]] = (
    (rule_reading.ANSWER_SCHEMA, "rules"),
    (tool_choice.ANSWER_SCHEMA, "tool_choice"),
    (checker_authoring.ANSWER_SCHEMA, "checker"),
    (checker_authoring.PYTHON_AST_SCHEMA, "checker"),
    (checker_authoring.AST_GREP_SCHEMA, "checker"),
    (judge.ANSWER_SCHEMA, "judge"),
    (fix_decision.ANSWER_SCHEMA, "decide"),
    (pr_check.PLAN_SCHEMA, "pr_plan"),
    (pr_check.JUDGE_SCHEMA, "pr_judge"),
)


def stage_of(schema: Mapping[str, object] | None) -> str:
    return next((name for known, name in STAGES if known is schema), "other")


def memo_key(prompt: str, system_prompt: str, schema: object, model: str) -> str:
    raw = json.dumps([prompt, system_prompt, schema, model], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def _result(row: Mapping[str, object]) -> StructuredResult:
    error = row["error"]
    return StructuredResult(**{**row, "error": None if error is None else AIErrorKind(error)})


class Memo:
    """한 파일에 쌓는 호출 기록. 성공한 답만 다시 쓴다(실패한 호출은 다음에 다시 묻는다)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.answers: dict[str, StructuredResult] = {}
        if path.is_file():
            for line in path.read_text().splitlines():
                row = json.loads(line)
                if row["kind"] == "call" and row["result"]["ok"]:
                    self.answers[row["key"]] = _result(row["result"])

    def _write(self, kind: Kind, **fields: object) -> None:
        with self.path.open("a") as out:
            out.write(json.dumps({"kind": kind, **fields}, ensure_ascii=False) + "\n")

    def structured(self, ask: Callable[..., StructuredResult], test: Callable[[], str]):
        def run(prompt: str, **kw) -> StructuredResult:
            schema, model = kw.get("schema"), kw.get("model", "")
            key = memo_key(prompt, kw.get("system_prompt", ""), schema, model)
            stage = stage_of(schema)
            if key in self.answers:
                self._write("hit", key=key, stage=stage, model=model, test=test())
                return self.answers[key]
            started = time.monotonic()
            result = ask(prompt, **kw)
            self._write("call", key=key, stage=stage, model=model, test=test(),
                        seconds=round(time.monotonic() - started, 1),
                        result={**asdict(result), "error": result.error})  # fmt: skip
            if result.ok:
                self.answers[key] = result
            return result

        return run

    def fix(self, run_claude: Callable[..., ClaudeResult], test: Callable[[], str]):
        def run(*args, **kw) -> ClaudeResult:
            started = time.monotonic()
            result = run_claude(*args, **kw)
            self._write("fix", model=kw.get("model", ""), test=test(), ok=result.ok,
                        model_id=result.model_id, input_tokens=result.input_tokens,
                        output_tokens=result.output_tokens, num_turns=result.num_turns,
                        seconds=round(time.monotonic() - started, 1))  # fmt: skip
            return result

        return run
