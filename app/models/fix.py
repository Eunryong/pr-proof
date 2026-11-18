"""수정안 판정 (docs/DESIGN.md F7·F8). 판정만 하고 파일을 바꾸지 않는다."""

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from app.models.finding import Finding, SkillResult
from app.models.test_run import PytestRun

FixStatus = Literal[
    "ready",  # 검증 통과. 제안할 수 있다
    "nothing_fixed",  # 적용할 수 있는 수정이 없었다
    "new_violations",  # 고친 뒤 원래 없던 위반이 생겼다
    "out_of_scope",  # 허용 범위 밖 파일이 바뀌었다
    "tests_changed",  # 고친 뒤 테스트 결과가 고치기 전과 다르다
    "unchecked",  # 고친 뒤 규칙 검사를 하지 못했다
]

# 수정 전후 비교에서 빼는 파이프라인 산출물
PIPELINE_OUTPUTS: Final = (".pipeline-out/",)


@dataclass(frozen=True)
class StatusEntry:
    """`git status --porcelain=v1 -z` 한 줄. 이름 변경이면 orig_path가 있다."""

    code: str  # XY 두 글자. 예: " M", "??", "R "
    path: str
    orig_path: str | None = None


@dataclass(frozen=True)
class FixOutcome:
    skill: str
    status: FixStatus
    fixed: int = 0  # 해소한 위반 수
    remaining: tuple[Finding, ...] = ()  # 고친 뒤에도 남은 PR의 위반
    test_run: PytestRun | None = None  # 고친 뒤 테스트
    changed_files: tuple[str, ...] = ()
    patch_path: Path | None = None  # 수정안 diff (제안 PR에 쓴다)
    detail: str = ""  # 버린 이유 등

    @property
    def ready(self) -> bool:
        return self.status == "ready"


def parse_porcelain(output: str) -> list[StatusEntry]:
    """`git status --porcelain=v1 -z` 출력. 이름 변경·복사는 다음 항목이 옛 경로다."""
    parts = output.split("\0")
    entries: list[StatusEntry] = []
    i = 0
    while i < len(parts):
        item = parts[i]
        i += 1
        if len(item) < 4:
            continue
        code, path = item[:2], item[3:]
        orig = None
        if code[0] in "RC" or code[1] in "RC":
            orig = parts[i] if i < len(parts) else None
            i += 1
        entries.append(StatusEntry(code=code, path=path, orig_path=orig))
    return entries


def out_of_scope(entries: Iterable[StatusEntry], allowed: Iterable[str]) -> list[str]:
    """허용 목록 밖의 변경. 수정(M)만 허용 파일에 허락하고, 새 파일·삭제·이름 변경은 모두 밖이다.

    lint 수정은 위반이 있는 파일의 내용만 바꾼다. 파일을 만들거나 지울 이유가 없다.
    """
    allowed_set = set(allowed)
    bad = []
    for e in entries:
        paths = [e.path] + ([e.orig_path] if e.orig_path else [])
        if all(p.startswith(PIPELINE_OUTPUTS) for p in paths):
            continue
        modified_only = set(e.code.strip()) == {"M"}
        if not modified_only or e.path not in allowed_set:
            bad.append(f"{e.code.strip()} {e.path}")
    return bad


def _key(f: Finding) -> tuple[str, str, str]:
    return (f.file, f.code, f.message)  # 줄 번호는 수정으로 밀리므로 비교에 쓰지 않는다


def compare(before: Sequence[Finding], after: SkillResult) -> tuple[int, list[Finding]]:
    """(해소한 수, 새로 생긴 위반). 같은 파일·규칙·메시지의 개수로 비교한다."""
    before_count = Counter(_key(f) for f in before)
    after_count = Counter(_key(f) for f in after.findings)
    new = []
    seen: Counter = Counter()
    for f in after.findings:
        seen[_key(f)] += 1
        if seen[_key(f)] > before_count[_key(f)]:
            new.append(f)
    fixed = sum((before_count - after_count).values())
    return fixed, new


def same_test_result(before: PytestRun, after: PytestRun) -> bool:
    """고친 뒤 테스트가 고치기 전과 같은 상태(통과 또는 테스트 없음)인지. 개수도 같아야 한다."""
    if before.status != after.status or not after.allows_lint_fix:
        return False
    if before.summary is None or after.summary is None:
        return before.summary is after.summary or after.status == "no_tests"
    return (before.summary.passed, before.summary.total) == (
        after.summary.passed,
        after.summary.total,
    )


FixBlocker = Literal["no_fixable", "tests"]


def lint_fix_blocker(lint: SkillResult | None, run: PytestRun) -> FixBlocker | None:
    """`/fix lint`를 제안할 수 없는 이유. 제안할 수 있으면 None (DESIGN.md F8 표)."""
    if lint is None or not lint.findings:
        return "no_fixable"
    if lint.fixable_count == 0:
        return "no_fixable"
    if not run.allows_lint_fix:
        return "tests"
    return None
