"""결과를 PR 코멘트용 마크다운으로 바꾼다. 판정을 새로 내지 않고 모델 값만 보여준다.

코멘트 맨 위는 "테스트:" 줄과 "규칙:" 줄이다. 테스트는 글자로만 쓰고, ⚠️/✅는 규칙 줄에만 쓰며
⚠️는 "위반 있음"이라는 뜻으로만 쓴다 (docs/DESIGN.md F9). 그 아래에 상세를 붙인다.
"""

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Final, Literal

from app.models.coverage import FixGate
from app.models.finding import Finding, SkillResult
from app.models.fix import FixBlocker, FixOutcome
from app.models.proposal import ProposalResult
from app.models.test_run import PytestRun
from app.sandbox.docker_cli import MEMORY_LIMIT_GB
from app.worker.base_config import PIPELINE_YAML, BaseConfig
from app.worker.coverage import DiffCoverage

MAX_LISTED_FAILURES: Final = 10
MAX_LISTED_FINDINGS: Final = 30
MAX_OUTPUT_CHARS: Final = 1500
MAX_PATCH_CHARS: Final = 6000

# 코멘트 node가 정하는 수정 제안 상태: 승인 대기, 승인 없이 바로 수정, 또는 제안하지 않는 이유
# merged: 제안을 머지해 생긴 커밋, rejected: 이 커밋의 제안이 거절됨 (다시 제안하지 않음),
# open: 이 커밋의 제안이 이미 열려 있음 (다시 권하지 않고 그 PR을 가리킴)
Proposal = Literal["approval", "auto", "merged", "rejected", "open"] | FixBlocker
SkillProposal = tuple[Proposal, int | None]  # (상태, 관련 제안 PR 번호)
FIX_COMMAND: Final = "/fix lint"
_FIX_STATUS_TEXT: Final[Mapping[str, str]] = {
    "nothing_fixed": "적용할 수 있는 수정이 없음",
    "new_violations": "수정안을 버림: 고친 뒤 원래 없던 위반이 생김",
    "out_of_scope": "수정안을 버림: 허용 범위 밖의 파일이 바뀜",
    "tests_changed": "수정안을 버림: 고친 뒤 테스트 결과가 달라짐",
    "unchecked": "수정안을 버림: 고친 뒤 규칙 검사를 하지 못함",
    "ai_failed": "수정안을 버림: 정해진 횟수 안에 검증을 통과하지 못함",
    "budget": "PR당 AI 예산을 다 써서 멈춤",
    "secret": "수정안을 버림: 비밀값으로 보이는 내용이 있음",
    "refused": "AI를 실행하지 않음",
}
_UNCOVERED_SHORT: Final = "테스트가 지나가지 않음"
_UNKNOWN_SHORT: Final = "커버리지를 알 수 없음"
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


def fix_command(skill: str) -> str:
    return f"/fix {skill}"


def proposal_suffix(proposals: Mapping[str, SkillProposal] | Proposal | None) -> str:
    """규칙 줄 끝에 붙는 수정 제안 (DESIGN.md F9: `· 자동 수정 가능: /fix lint`)."""
    proposals = _as_mapping(proposals)
    waiting = [s for s, (p, _) in proposals.items() if p == "approval"]
    auto = [s for s, (p, _) in proposals.items() if p == "auto"]
    parts = []
    if waiting:
        parts.append("자동 수정 가능: " + ", ".join(f"`{fix_command(s)}`" for s in waiting))
    if auto:
        parts.append("자동 수정 중")
    return "".join(f" · {part}" for part in parts)


def _as_mapping(proposals) -> Mapping[str, SkillProposal]:
    """예전 호출(lint 상태 하나)도 받는다."""
    if proposals is None:
        return {}
    if isinstance(proposals, str):
        return {"lint": (proposals, None)}
    return proposals


def _pr_ref(number: int | None) -> str:
    return f"(#{number})" if number is not None else ""


def proposal_details(
    proposal: Proposal | None,
    run: PytestRun,
    number: int | None = None,
    skill: str = "lint",
    gate: FixGate | None = None,
    lint: SkillResult | None = None,
) -> list[str]:
    ref = _pr_ref(number)
    if proposal == "merged":
        return [
            "",
            f"이 커밋은 {skill} 제안 PR{ref}을 머지한 결과라 같은 제안을 다시 만들지 않습니다.",
        ]
    if proposal == "rejected":
        return [
            "",
            f"이 커밋의 {skill} 제안 PR{ref}이 머지되지 않고 닫혀 다시 제안하지 않습니다.",
        ]
    if proposal == "open":
        return ["", f"이 커밋의 {skill} 제안 PR{ref}이 열려 있습니다. 그 PR을 확인해 주세요."]
    if proposal == "tests":
        if skill != "lint" and gate is not None and gate.blocked_reason:
            return ["", f"{skill} 자동 수정을 제안하지 않습니다 ({gate.blocked_reason})."]
        return [
            "",
            f"{'' if skill == 'lint' else skill + ' '}"
            f"테스트가 통과하지 않아 자동 수정을 제안하지 않습니다 ({format_test_line(run)}).",
        ]
    if proposal == "approval" and skill == "lint":
        findings = lint.findings if lint is not None else ()
        fixable = [f for f in findings if f.fixable]
        if lint is None:
            what = "ruff의 안전한 수정 중 PR이 바꾼 줄의 것만 적용하고"
        elif len(fixable) == len(findings):
            what = f"lint 위반 {len(fixable)}건을 ruff의 안전한 수정으로 고치고"
        else:
            n, safe = len(findings), len(fixable)
            what = f"lint 위반 {n}건 중 ruff의 안전한 수정이 있는 {safe}건을 고치고"
        lines = [
            "",
            f"`{FIX_COMMAND}`를 코멘트로 달면 {what}, 테스트로 확인한 뒤 수정안을 만듭니다.",
        ]
        if lint is not None and len(fixable) < len(findings):
            lines += ["", target_locations("lint", fixable)]
        return lines
    if proposal == "approval":
        n = len(gate.fixable) if gate is not None else 0
        return [
            "",
            f"`{fix_command(skill)}`를 코멘트로 달면 AI가 테스트가 지나가는 {skill} 위반 {n}건을 "
            "고치고, 규칙 재검사와 테스트로 확인한 뒤 수정안을 만듭니다.",
        ]
    return []


def gate_details(gate: FixGate | None, skill: str = "static") -> list[str]:
    """자동 수정 대상이 아닌 위반은 이유별 개수만, 수정이 실제로 고칠 위반은 위치만 적는다 (F8·F9).

    위반 내용은 위 목록에 이미 있으므로 다시 적지 않는다. 대상이 전부면 따로 적지 않는다.
    """
    if gate is None or not gate.allowed:
        return []
    reasons = [(len(gate.uncovered), _UNCOVERED_SHORT), (len(gate.unknown), _UNKNOWN_SHORT)]
    skipped = sum(n for n, _ in reasons)
    if not skipped:
        return []
    by_reason = ", ".join(f"{why} {n}건" for n, why in reasons if n)
    lines = ["", f"자동 수정 대상이 아닌 위반 {skipped}건: {by_reason}"]
    if gate.fixable:
        lines += ["", target_locations(skill, gate.fixable)]
    return lines


def target_locations(skill: str, targets: Sequence[Finding]) -> str:
    """수정이 고칠 위반의 위치만 한 줄로. 내용은 위 위반 목록에 이미 있다."""
    shown = ", ".join(f"`{f.file}:{f.line}`" for f in targets[:MAX_LISTED_FINDINGS])
    hidden = len(targets) - MAX_LISTED_FINDINGS
    more = f" 외 {hidden}건" if hidden > 0 else ""
    return f"`{fix_command(skill)}`이 고칠 위반 {len(targets)}건: {shown}{more}"


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
        lines += ai_cost_lines(fix)
        if fix.skill != "lint":  # lint 수정은 결정적이라 같은 커밋에서 다시 돌려도 같다
            lines.append(f"`{fix_command(fix.skill)}`를 코멘트로 달면 AI 수정을 다시 돌립니다.")
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
    lines += ai_cost_lines(fix)
    if result is not None and result.secret_blocked:
        return lines  # 비밀값으로 보이는 줄을 코멘트에 옮기지 않는다
    if fix.explanation:  # AI가 쓴 글이라 코드 블록에 넣는다 (멘션·마크업이 동작하지 않게)
        lines += _details("AI의 변경 설명", fix.explanation)
    if fix.patch_path is not None and fix.patch_path.is_file():
        patch = fix.patch_path.read_text()
        if len(patch) > MAX_PATCH_CHARS:
            patch = patch[:MAX_PATCH_CHARS] + "\n… (생략)"
        fence = code_fence(patch)
        lines += ["", "<details><summary>수정 내용</summary>", "", f"{fence}diff", patch, fence, "",
                  "</details>"]  # fmt: skip
    return lines


def ai_cost_lines(fix: FixOutcome) -> list[str]:
    """이번 검사의 AI 비용 (DESIGN.md 비용 최적화 6). 비용을 모르는 호출은 호출당 상한으로 센다."""
    if not fix.ai_calls:
        return []
    models = ", ".join(fix.models)
    return [f"AI 호출 {fix.ai_calls}회 ({models}) · 추정 비용 ${fix.ai_cost_usd:.4f}"]


def code_fence(text: str) -> str:
    """text 안의 어떤 백틱 줄보다 긴 코드 울타리. 본문의 ```가 블록을 일찍 닫지 않게 한다."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


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
    names = {"lint": "ruff", "static": "정적변수"}
    sources = [f"{names[r.skill]} ({r.source})" for r in results if r.skill in names and r.source]
    if sources:
        lines += ["", f"적용한 규칙: {', '.join(sources)} · PR이 바꾼 줄만 검사"]
    return lines


# --- PR 코멘트 전체 -------------------------------------------------------------

HEADER: Final = "### pr-proof"


def _details(summary: str, text: str) -> list[str]:
    fence = code_fence(text)
    return ["", f"<details><summary>{summary}</summary>", "", fence, text, fence, "", "</details>"]


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
    static: SkillResult | None = None,
    seconds: float | None = None,
    from_sync: bool = False,
    suffix: str = "",
    extra: list[str] | None = None,
) -> str:
    results = [r for r in (lint, static) if r is not None]
    lines = [HEADER, "", f"**테스트:** {test_line}"]
    if results:
        lines.append(f"**규칙:** {format_rules_line(results)}{suffix}")
    lines += details
    lines += rules_details(results)
    lines += extra or []
    off = [s for s in ("lint", "static") if base is not None and s not in base.repo.skills.enabled]
    if off:
        lines += [
            "",
            f"이 리포에서 꺼진 규칙 스킬: {', '.join(off)} "
            "(base 브랜치 `.pipeline.yaml`의 `skills.enabled`).",
        ]
    if base is not None and base.problems:
        lines += ["", "설정:"] + [f"- {p}" for p in base.problems]
    lines += ["", _footer(head_sha, seconds, from_sync=from_sync)]
    return "\n".join(lines) + "\n"


def render_progress(head_sha: str, step: str) -> str:
    return f"{HEADER}\n\n**테스트:** 진행 중 ({step})\n\n{_footer(head_sha)}\n"


def diff_coverage_line(cov: DiffCoverage | None) -> list[str]:
    """PR이 바꾼 줄의 커버리지 (참고 지표, F8). 잴 수 없으면 쓰지 않는다."""
    if cov is None:
        return []
    return [
        "",
        f"바뀐 줄 커버리지 (참고): {cov.percent:g}% "
        f"(테스트가 센 {cov.measured_lines}줄 중 {cov.uncovered_lines}줄이 실행되지 않음)",
    ]


def render_result(
    run: PytestRun,
    head_sha: str,
    seconds: float,
    *,
    lint: SkillResult | None = None,
    static: SkillResult | None = None,
    base: BaseConfig | None = None,
    proposals: Mapping[str, SkillProposal] | None = None,
    static_gate: FixGate | None = None,
    diff_coverage: DiffCoverage | None = None,
    fixes: Mapping[str, FixOutcome] | None = None,
    proposal_results: Mapping[str, ProposalResult] | None = None,
    from_sync: bool = False,
    # lint 하나만 쓰던 호출 (2단계)
    proposal: Proposal | None = None,
    proposal_number: int | None = None,
    fix: FixOutcome | None = None,
    proposal_result: ProposalResult | None = None,
) -> str:
    """결과 코멘트. 스킬마다 수정을 했으면 그 결과, 아니면 제안 상태를 적는다."""
    memory_gb = base.memory_gb if base else MEMORY_LIMIT_GB
    max_memory_gb = base.max_memory_gb if base else MEMORY_LIMIT_GB
    proposals = dict(proposals or {})
    if proposal is not None:
        proposals.setdefault("lint", (proposal, proposal_number))
    fixes = dict(fixes or {})
    if fix is not None:
        fixes.setdefault(fix.skill, fix)
    results = dict(proposal_results or {})
    if proposal_result is not None and fix is not None:
        results.setdefault(fix.skill, proposal_result)
    extra = gate_details(static_gate) + diff_coverage_line(diff_coverage)
    for skill in ("lint", "static"):
        if skill in fixes:
            extra += fix_section(fixes[skill], results.get(skill))
        elif skill in proposals:
            state, number = proposals[skill]
            gate = static_gate if skill == "static" else None
            extra += proposal_details(state, run, number, skill, gate, lint=lint)
    waiting = {s: v for s, v in proposals.items() if s not in fixes}
    return _compose(
        format_test_line(run, memory_gb),
        format_test_details(run, memory_gb, max_memory_gb),
        head_sha,
        lint=lint,
        static=static,
        base=base,
        seconds=seconds,
        from_sync=from_sync,
        suffix=proposal_suffix(waiting),
        extra=extra,
    )


def render_install_failure(
    head_sha: str,
    detail: str,
    *,
    lint: SkillResult | None = None,
    static: SkillResult | None = None,
    base: BaseConfig | None = None,
) -> str:
    """의존성 설치 실패. 규칙 검사는 설치와 따로 돌므로 결과가 있으면 같이 보여준다."""
    return _compose(
        "실행 불가 (의존성을 설치하지 못함)",
        _details("pip 출력", detail[-MAX_OUTPUT_CHARS:]),
        head_sha,
        lint=lint,
        static=static,
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
