"""결과를 PR 코멘트용 마크다운으로 바꾼다. 판정을 새로 내지 않고 모델 값만 보여준다.

코멘트 맨 위는 "테스트:" 줄과 "규칙:" 줄이다. 테스트는 글자로만 쓰고, ⚠️/✅는 규칙 줄에만 쓰며
⚠️는 "위반 있음"이라는 뜻으로만 쓴다 (docs/DESIGN.md F9). 그 아래에 상세를 붙인다.
"""

from collections import Counter
from collections.abc import Mapping
from typing import Final, Literal

from app.models.finding import SkillResult
from app.models.fix import FixBlocker, FixOutcome
from app.models.proposal import ProposalResult
from app.models.test_run import PytestRun
from app.sandbox.docker_cli import MEMORY_LIMIT_GB
from app.worker.base_config import PIPELINE_YAML, BaseConfig

MAX_LISTED_FAILURES: Final = 10
MAX_LISTED_FINDINGS: Final = 30
MAX_OUTPUT_CHARS: Final = 1500
MAX_PATCH_CHARS: Final = 6000

# 코멘트 node가 정하는 수정 제안 상태: 승인 대기, 승인 없이 바로 수정, 또는 제안하지 않는 이유
# merged: 제안을 머지해 생긴 커밋, rejected: 이 커밋의 제안이 거절됨 (다시 제안하지 않음)
Proposal = Literal["approval", "auto", "merged", "rejected"] | FixBlocker
FIX_COMMAND: Final = "/fix lint"
_FIX_STATUS_TEXT: Final[Mapping[str, str]] = {
    "nothing_fixed": "적용할 수 있는 수정이 없음",
    "new_violations": "수정안을 버림: 고친 뒤 원래 없던 위반이 생김",
    "out_of_scope": "수정안을 버림: 허용 범위 밖의 파일이 바뀜",
    "tests_changed": "수정안을 버림: 고친 뒤 테스트 결과가 달라짐",
    "unchecked": "수정안을 버림: 고친 뒤 규칙 검사를 하지 못함",
}
VIOLATION: Final = "⚠️"
CLEAN: Final = "✅"


def _gb(value: float) -> str:
    return f"{value:g}GB"


# --- 테스트 ---------------------------------------------------------------------


def format_test_line(run: PytestRun, memory_gb: float = MEMORY_LIMIT_GB) -> str:
    """'테스트:' 줄의 글자 표시. 기호를 쓰지 않는다."""
    s = run.summary
    match run.status:
        case "passed":
            if run.flaky:
                return f"통과, 불안정한 테스트 {len(run.flaky)}개"
            return f"통과 ({s.total}개)" if s else "통과"
        case "failed":
            if s is None:
                return "실패"
            return f"실패 ({s.total}개 중 {s.failed + s.errors}개)"
        case "no_tests":
            return "실행된 테스트 없음"
        case "timeout":
            return "시간 초과로 중단"
        case "cancelled":
            return "새 커밋이 올라와 중단"
        case "unrunnable":
            if run.memory_exceeded:
                return f"실행 불가 (메모리 한도 {_gb(memory_gb)}를 넘어 종료됨)"
            return f"실행 불가 ({run.unrunnable_reason})"


def memory_hint(memory_gb: float, max_memory_gb: float) -> str:
    """메모리 초과 안내. 한도는 base 브랜치의 설정에서만 바꿀 수 있다 (DESIGN.md F8)."""
    if memory_gb >= max_memory_gb:
        return (
            f"샌드박스의 메모리 한도는 {_gb(memory_gb)}이며, "
            f"이 파이프라인이 허용하는 상한({_gb(max_memory_gb)})입니다."
        )
    return (
        f"샌드박스의 메모리 한도는 {_gb(memory_gb)}입니다. base 브랜치의 `{PIPELINE_YAML}`에서 "
        f"`sandbox.memory_gb`로 {_gb(max_memory_gb)}까지 늘릴 수 있습니다."
    )


def format_test_details(run: PytestRun, memory_gb: float, max_memory_gb: float) -> list[str]:
    """'테스트:' 줄 아래에 붙는 상세 줄들. 비어 있을 수 있다."""
    lines: list[str] = []
    if run.memory_exceeded:
        lines += ["", memory_hint(memory_gb, max_memory_gb)]
    s = run.summary
    if s is not None and s.total:
        lines += [
            "",
            f"전체 {s.total} · 통과 {s.passed} · 실패 {s.failed}"
            f" · 오류 {s.errors} · 건너뜀 {s.skipped}",
        ]
    if run.flaky:
        lines += ["", "불안정한 테스트 (처음엔 실패, 다시 돌려 통과):"]
        lines += [f"- `{t}`" for t in run.flaky[:MAX_LISTED_FAILURES]]
    still_failed = [t for t in (s.failed_tests if s else ()) if t not in run.flaky]
    if s is not None and still_failed:
        lines += ["", "실패한 테스트:"]
        lines += [f"- `{t}`" for t in still_failed[:MAX_LISTED_FAILURES]]
        hidden = len(still_failed) - MAX_LISTED_FAILURES
        if hidden > 0:
            lines.append(f"- 외 {hidden}개")
    if run.status == "unrunnable" and run.output:
        lines += _details("출력", run.output[-MAX_OUTPUT_CHARS:])
    return lines


# --- 규칙 -----------------------------------------------------------------------


def format_rules_line(results: list[SkillResult]) -> str:
    """'규칙:' 줄. ⚠️는 위반이 있을 때만, 검사하지 못한 스킬은 글자로 적는다."""
    findings = [f for r in results for f in r.findings]
    unchecked = [r.skill for r in results if not r.checked]
    if findings:
        counts = Counter(f.skill for f in findings)
        by_skill = ", ".join(f"{skill} {n}" for skill, n in counts.items())
        line = f"{VIOLATION} 위반 {len(findings)}건 ({by_skill})"
    elif unchecked:
        line = "검사하지 못함"
    else:
        line = f"{CLEAN} 위반 없음"
    if unchecked and findings:
        line += f" · 검사하지 못함: {', '.join(unchecked)}"
    elif unchecked:
        line += f" ({', '.join(unchecked)})"
    return line


def proposal_suffix(proposal: Proposal | None) -> str:
    """규칙 줄 끝에 붙는 수정 제안 (DESIGN.md F9: `· 자동 수정 가능: /fix lint`)."""
    if proposal == "approval":
        return f" · 자동 수정 가능: `{FIX_COMMAND}`"
    if proposal == "auto":
        return " · 자동 수정 중"
    return ""


def proposal_details(proposal: Proposal | None, run: PytestRun) -> list[str]:
    if proposal == "merged":
        return ["", "이 커밋은 lint 제안 PR을 머지한 결과라 같은 제안을 다시 만들지 않습니다."]
    if proposal == "rejected":
        return ["", "이 커밋의 lint 제안 PR이 머지되지 않고 닫혀 다시 제안하지 않습니다."]
    if proposal == "tests":
        return [
            "",
            f"테스트가 통과하지 않아 자동 수정을 제안하지 않습니다 ({format_test_line(run)}).",
        ]
    if proposal == "approval":
        return [
            "",
            f"`{FIX_COMMAND}`을 코멘트로 달면 ruff의 안전한 수정 중 PR이 바꾼 줄의 것만 적용하고, "
            "테스트로 확인한 뒤 수정안을 만듭니다.",
        ]
    return []


def proposal_line(result: ProposalResult | None) -> str:
    if result is None:
        return ""
    if result.number is not None:
        verb = "갱신했습니다" if result.updated else "열었습니다"
        return f" 제안 PR #{result.number}을 {verb}."
    return f" 제안 PR을 만들지 않았습니다 ({result.reason})."


def fix_section(fix: FixOutcome, result: ProposalResult | None = None) -> list[str]:
    """수정안 결과. 버린 수정안은 이유만 적는다."""
    if not fix.ready:
        reason = _FIX_STATUS_TEXT.get(fix.status, fix.status)
        lines = ["", f"**{fix.skill} 수정안:** {reason}"]
        if fix.detail:
            lines[-1] += f" ({fix.detail})"
        return lines
    tests = format_test_line(fix.test_run) if fix.test_run else "확인 안 함"
    what = f"위반 {fix.fixed}건을 고쳤습니다" if fix.fixed else "포맷만 고쳤습니다"
    lines = [
        "",
        f"**{fix.skill} 수정안:** {what}. 고친 뒤 테스트: {tests}.{proposal_line(result)}",
    ]
    if fix.formatted_files:
        lines.append(f"`ruff format`을 PR이 바꾼 파일 {len(fix.formatted_files)}개에 적용했습니다.")
    elif fix.format_skipped:
        lines.append(f"`ruff format`은 적용하지 않았습니다 ({fix.format_skipped}).")
    if fix.remaining:
        lines.append(f"자동으로 고치지 못한 위반 {len(fix.remaining)}건은 그대로 남습니다.")
    if fix.patch_path is not None and fix.patch_path.is_file():
        patch = fix.patch_path.read_text()
        if len(patch) > MAX_PATCH_CHARS:
            patch = patch[:MAX_PATCH_CHARS] + "\n… (생략)"
        lines += ["", "<details><summary>수정 내용</summary>", "", "```diff", patch, "```", "",
                  "</details>"]  # fmt: skip
    return lines


def rules_details(results: list[SkillResult]) -> list[str]:
    lines: list[str] = []
    for r in results:
        if r.error is not None:
            lines += ["", f"{r.skill}: 검사하지 못함 ({r.error})"]
            continue
        if r.findings:
            lines += ["", f"{r.skill} 위반:"]
            lines += [
                f"- `{f.file}:{f.line}` `{f.code}` {f.message}"
                for f in r.findings[:MAX_LISTED_FINDINGS]
            ]
            hidden = len(r.findings) - MAX_LISTED_FINDINGS
            if hidden > 0:
                lines.append(f"- 외 {hidden}건")
    sources = [f"ruff ({r.source})" for r in results if r.skill == "lint" and r.source]
    if sources:
        lines += ["", f"적용한 규칙: {', '.join(sources)} · PR이 바꾼 줄만 검사"]
    return lines


# --- PR 코멘트 전체 -------------------------------------------------------------

HEADER: Final = "### pr-proof"


def _details(summary: str, text: str) -> list[str]:
    return ["", f"<details><summary>{summary}</summary>", "", "```", text, "```", "", "</details>"]


def _footer(head_sha: str, seconds: float | None = None, *, from_sync: bool = False) -> str:
    parts = [f"커밋 `{head_sha[:7]}`"]
    if seconds is not None:
        parts.append(f"{seconds:.0f}초")
    if from_sync:
        parts.append("동기화로 처리됨 (웹훅 누락)")
    return "<sub>" + " · ".join(parts) + "</sub>"


def _compose(
    test_line: str,
    details: list[str],
    head_sha: str,
    *,
    lint: SkillResult | None,
    base: BaseConfig | None,
    seconds: float | None = None,
    from_sync: bool = False,
    suffix: str = "",
    extra: list[str] | None = None,
) -> str:
    results = [lint] if lint is not None else []
    lines = [HEADER, "", f"**테스트:** {test_line}"]
    if results:
        lines.append(f"**규칙:** {format_rules_line(results)}{suffix}")
    lines += details
    lines += rules_details(results)
    lines += extra or []
    if base is not None and base.problems:
        lines += ["", "설정:"] + [f"- {p}" for p in base.problems]
    lines += ["", _footer(head_sha, seconds, from_sync=from_sync)]
    return "\n".join(lines) + "\n"


def render_progress(head_sha: str, step: str) -> str:
    return f"{HEADER}\n\n**테스트:** 진행 중 ({step})\n\n{_footer(head_sha)}\n"


def render_result(
    run: PytestRun,
    head_sha: str,
    seconds: float,
    *,
    lint: SkillResult | None = None,
    base: BaseConfig | None = None,
    proposal: Proposal | None = None,
    fix: FixOutcome | None = None,
    proposal_result: ProposalResult | None = None,
    from_sync: bool = False,
) -> str:
    memory_gb = base.memory_gb if base else MEMORY_LIMIT_GB
    max_memory_gb = base.max_memory_gb if base else MEMORY_LIMIT_GB
    if fix is not None:
        extra = fix_section(fix, proposal_result)
    else:
        extra = proposal_details(proposal, run)
    return _compose(
        format_test_line(run, memory_gb),
        format_test_details(run, memory_gb, max_memory_gb),
        head_sha,
        lint=lint,
        base=base,
        seconds=seconds,
        from_sync=from_sync,
        suffix=proposal_suffix(proposal) if fix is None else "",
        extra=extra,
    )


def render_install_failure(
    head_sha: str, detail: str, *, lint: SkillResult | None = None, base: BaseConfig | None = None
) -> str:
    """의존성 설치 실패. 규칙 검사는 설치와 따로 돌므로 결과가 있으면 같이 보여준다."""
    return _compose(
        "실행 불가 (의존성을 설치하지 못함)",
        _details("pip 출력", detail[-MAX_OUTPUT_CHARS:]),
        head_sha,
        lint=lint,
        base=base,
    )


def render_environment_wait(head_sha: str, *, final: bool) -> str:
    """환경 오류. 실패처럼 보이는 표시를 남기지 않는다 (DESIGN.md F9)."""
    if final:
        line = "환경 문제로 검사하지 못함 (다음 커밋에서 다시 검사합니다)"
    else:
        line = "환경 문제로 대기 중 (잠시 뒤 다시 시도합니다)"
    return f"{HEADER}\n\n**테스트:** {line}\n\n{_footer(head_sha)}\n"


def render_pipeline_error(head_sha: str, *, final: bool) -> str:
    """환경 오류가 아닌 예외(파이프라인 버그, 작업 시간 초과). PR 코드 탓으로 보이지 않게 쓴다."""
    if final:
        line = "파이프라인 오류로 검사하지 못함 (다음 커밋에서 다시 검사합니다)"
    else:
        line = "파이프라인 오류로 대기 중 (잠시 뒤 다시 시도합니다)"
    return f"{HEADER}\n\n**테스트:** {line}\n\n{_footer(head_sha)}\n"
