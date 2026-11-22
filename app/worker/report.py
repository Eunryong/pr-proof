"""결과를 PR 코멘트용 마크다운으로 바꾼다. 판정을 새로 내지 않고 모델 값만 보여준다.

코멘트 맨 위는 "테스트:" 줄과 결론인 "규칙:" 줄이고, PR 단위 위반이 있으면 "PR 단위:" 줄이 하나 더
붙는다. 테스트는 글자로만 쓰고, ⚠️/✅는 규칙 줄에만 쓰며 ⚠️는 "위반 있음"이라는 뜻으로만 쓴다
(docs/DESIGN.md 4.9). 그 아래에는 위반과 할 일만 펼쳐 두고, 규칙별 확인 방법, 사용량, 테스트 수,
바뀐 줄 커버리지는 접는 블록 하나에 넣는다. 규칙 줄과 규칙 절은 app/worker/doc_rules_report.py가
그린다.
"""

from collections.abc import Mapping, Sequence
from typing import Final

from app.worker import doc_rules_report, pr_check_report_pure
from app.worker.base_config import PIPELINE_YAML, BaseConfig
from app.worker.checker_pure import Checker
from app.worker.coverage import DiffCoverage
from app.worker.doc_rules_pure import DocRules, Found
from app.worker.doc_rules_report import FixView
from app.worker.fix.fix_decision_pure import Decided
from app.worker.lang.install_pure import InstallFailureKind
from app.worker.markdown_pure import code_fence, inline_code
from app.worker.merged_fix_pure import CarriedFrom
from app.worker.rag_switch_pure import RagSwitch
from app.worker.rules.pr_check_pure import PrChecks
from app.worker.sandbox.docker_cli import MEMORY_LIMIT_GB
from app.worker.test_run_pure import PytestRun

MAX_LISTED_FAILURES: Final = 10
MAX_OUTPUT_CHARS: Final = 1500


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


def format_test_counts(run: PytestRun) -> list[str]:
    """테스트 수의 상세 (접는 블록 안). 통과·실패는 '테스트:' 줄에 이미 보인다."""
    s = run.summary
    if s is None or not s.total:
        return []
    return ["", f"테스트: 전체 {s.total} · 통과 {s.passed} · 실패 {s.failed} · 오류 {s.errors} · "
            f"건너뜀 {s.skipped}"]  # fmt: skip


def format_test_details(run: PytestRun, memory_gb: float, max_memory_gb: float) -> list[str]:
    """'테스트:' 줄 아래에 펼쳐 두는 상세 줄들: 할 일이 있는 것만. 비어 있을 수 있다."""
    lines: list[str] = []
    if run.memory_exceeded:
        lines += ["", memory_hint(memory_gb, max_memory_gb)]
    s = run.summary
    if run.flaky:
        lines += ["", "불안정한 테스트 (처음엔 실패, 다시 돌려 통과):"]
        lines += [f"- {inline_code(t, 300)}" for t in run.flaky[:MAX_LISTED_FAILURES]]
    still_failed = [t for t in (s.failed_tests if s else ()) if t not in run.flaky]
    if s is not None and still_failed:
        lines += ["", "실패한 테스트:"]
        lines += [f"- {inline_code(t, 300)}" for t in still_failed[:MAX_LISTED_FAILURES]]
        hidden = len(still_failed) - MAX_LISTED_FAILURES
        if hidden > 0:
            lines.append(f"- 외 {hidden}개")
    if run.status == "unrunnable" and run.output:
        lines += _details("출력", run.output[-MAX_OUTPUT_CHARS:])
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
    base: BaseConfig | None,
    seconds: float | None = None,
    from_sync: bool = False,
    folded: list[str] | None = None,
    doc_rules: DocRules | None = None,
    checkers: Mapping[str, Checker] | None = None,
    decisions: Decided | None = None,
    fixes: Mapping[str, FixView] | None = None,
    rag: RagSwitch | None = None,
    pr: PrChecks | None = None,
) -> str:
    lines = [HEADER, "", f"**테스트:** {test_line}"]
    rules_line = doc_rules_report.title(doc_rules, fixes, pr, decisions)
    if rules_line is not None:
        lines.append(rules_line)
    pr_line = pr_check_report_pure.title(pr) if isinstance(doc_rules, Found) else None
    if pr_line is not None:
        lines.append(pr_line)
    lines += details
    lines += doc_rules_report.section(
        doc_rules, checkers or {}, decisions, fixes, rag, pr, more=folded or []
    )
    if base is not None and base.problems:
        lines += ["", "설정:"] + [f"- {p}" for p in base.problems]
    lines += ["", _footer(head_sha, seconds, from_sync=from_sync)]
    return "\n".join(lines) + "\n"


def render_progress(head_sha: str, step: str) -> str:
    return f"{HEADER}\n\n**테스트:** 진행 중 ({step})\n\n{_footer(head_sha)}\n"


def diff_coverage_line(cov: DiffCoverage | None) -> list[str]:
    """PR이 바꾼 줄의 커버리지 (참고 지표, F8). 잴 수 없으면 쓰지 않는다. 위반이 아니라 있던
    테스트가 바뀐 코드에 얼마나 닿는지를 알려 주는 값이라 접는 블록에 들어간다 (4.9)."""
    if cov is None:
        return []
    ran = cov.measured_lines - cov.uncovered_lines
    return [
        "",
        f"바뀐 줄 커버리지 {cov.percent:g}% (참고, 위반이 아님): 있던 테스트가 PR이 바꾼 줄 "
        f"{cov.measured_lines}줄 중 {ran}줄을 실행함",
    ]


_CARRIED_LABEL: Final[Mapping[str, str]] = {"mechanical": "기계적 수정", "model": "모델 수정"}


def carried_lines(carried: CarriedFrom | None) -> list[str]:
    """수정안을 머지한 커밋이라 다시 검사하지 않고 옮겨 적었다는 안내 (4.9 "수정안 PR 상태
    관리")."""
    if carried is None:
        return []
    n, verified = carried.number, carried.verified_sha[:7]
    label = _CARRIED_LABEL.get(carried.bundle, carried.bundle)
    return [
        "",
        f"이 커밋은 수정안 #{n}({label})을 머지한 커밋이고, 내용이 검증한 수정안과 같아 다시 "
        f"검사하지 않았습니다. 결과는 커밋 `{verified}`의 검사와 수정안 #{n}의 검증에서 옮겨 "
        "적었습니다. 테스트는 수정안을 검증할 때 고친 뒤 다시 돌려 수정 전과 같은 결과를 확인한 "
        "것입니다.",
        *(
            [
                f"수정안이 고친 위반 중 {carried.unverified}건은 모델 판단으로 찾은 것이라 도구로 "
                "재검사하지 못했고, 그 줄이 바뀐 것까지만 확인했습니다."
            ]
            if carried.unverified
            else []
        ),
    ]


def render_result(
    run: PytestRun,
    head_sha: str,
    seconds: float,
    *,
    base: BaseConfig | None = None,
    diff_coverage: DiffCoverage | None = None,
    from_sync: bool = False,
    doc_rules: DocRules | None = None,
    checkers: Mapping[str, Checker] | None = None,
    decisions: Decided | None = None,
    fixes: Mapping[str, FixView] | None = None,
    rag: RagSwitch | None = None,
    pr: PrChecks | None = None,
    carried: CarriedFrom | None = None,
) -> str:
    """결과 코멘트. fixes는 묶음마다 수정했으면 그 결과, 아니면 상태(대기, 진행 중 등)다. rag는 이
    검사의 검색 상태다(규칙 절에 한 줄로 보인다). pr은 PR 단위 확인의 결과다. carried는 수정안을
    머지한 커밋이라 결과를 옮겨 적었을 때 그 출처다(테스트 줄 아래에 안내가 붙는다)."""
    memory_gb = base.memory_gb if base else MEMORY_LIMIT_GB
    max_memory_gb = base.max_memory_gb if base else MEMORY_LIMIT_GB
    return _compose(
        format_test_line(run, memory_gb),
        carried_lines(carried) + format_test_details(run, memory_gb, max_memory_gb),
        head_sha,
        base=base,
        seconds=seconds,
        from_sync=from_sync,
        folded=format_test_counts(run) + diff_coverage_line(diff_coverage),
        doc_rules=doc_rules,
        checkers=checkers,
        decisions=decisions,
        fixes=fixes,
        rag=rag,
        pr=pr,
    )


def render_install_failure(
    head_sha: str,
    detail: str,
    *,
    base: BaseConfig | None = None,
    doc_rules: DocRules | None = None,
    checkers: Mapping[str, Checker] | None = None,
    kind: InstallFailureKind = "unknown",
    packages: Sequence[str] = (),
    pr: PrChecks | None = None,
) -> str:
    """의존성 설치 실패. 규칙 검사는 설치와 따로 돌므로 결과가 있으면 같이 보여준다."""
    return _compose(
        f"실행 불가 ({install_failure_reason(kind, packages)})",
        _details("pip 출력", detail[-MAX_OUTPUT_CHARS:]),
        head_sha,
        base=base,
        doc_rules=doc_rules,
        checkers=checkers,
        pr=pr,
    )


def install_failure_reason(kind: InstallFailureKind, packages: Sequence[str] = ()) -> str:
    if kind == "source_only" and packages:
        names = ", ".join(inline_code(p, 100) for p in packages[:10])
        return f"의존성을 설치하지 못함: 소스 배포본만 있는 패키지 {names}. wheel만 설치합니다"
    if kind == "disallowed":
        return "의존성을 설치하지 못함: wheel만 설치하므로 받지 않는 줄이 의존성 파일에 있음"
    return "의존성을 설치하지 못함"


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
