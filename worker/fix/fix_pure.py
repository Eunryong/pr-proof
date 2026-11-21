"""기계적 수정 묶음의 결과와 수정 뒤 테스트의 판정 (docs/DESIGN.md 4.7 기계적 수정, 4.8).
판정만 하고 파일을 바꾸지 않는다."""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Literal

from worker.fix.scope_pure import MECHANICAL_POLICY, StatusEntry, judge_scope
from worker.test_run_pure import PytestRun

MechanicalStatus = Literal[
    "ready",  # 검증 통과. 수정안 PR로 올린다
    "nothing_fixed",  # 적용할 수 있는 수정이 없었다
    "new_violations",  # 고친 뒤 원래 없던 위반이 생겼다
    "out_of_scope",  # 허용 범위 밖 파일이 바뀌었다
    "secret",  # 바뀐 줄에 비밀값으로 보이는 것이 있다
    "tests_changed",  # 고친 뒤 테스트 결과가 고치기 전과 다르다
    "unchecked",  # 고친 뒤 규칙 재검사를 하지 못했다
    "too_big",  # 크기 상한을 넘었다
]
# 기계적 수정 묶음 (3.1의 C7). 브랜치는 pipeline/pr-<n>-fix-mechanical
MECHANICAL_BUNDLE: Final = "mechanical"


@dataclass(frozen=True)
class MechanicalFix:
    """기계적 수정 묶음의 결과 (4.7 출력, 5.5 fixes["mechanical"]). 패치 본문은 상태에 넣지 않고
    `fix_attempts`의 id만 둔다."""

    status: MechanicalStatus
    targets: tuple[str, ...]  # 수정 판단이 `fix`·`mechanical`로 정한 위반 id
    fixed: tuple[str, ...] = ()  # 고친 뒤 없어진 대상
    remaining: tuple[str, ...] = ()  # 고친 뒤에도 남은 대상 (안전한 수정이 겹쳐 회차를 다 씀 등)
    changed_files: tuple[str, ...] = ()
    formatted_files: tuple[str, ...] = ()  # ruff format으로 바뀐 파일
    format_skipped: str = ""  # 포맷하지 않은 이유
    tests: int = 0  # 고친 뒤 수정 전과 같은 결과로 통과한 테스트 수
    test_line: str = ""  # 고친 뒤 테스트의 글자 표시
    detail: str = ""  # 버린 이유 등
    attempt_id: int | None = None  # 저장한 시도 (`fix_attempts`)

    @property
    def ready(self) -> bool:
        return self.status == "ready"


# 체크포인트에서 되살릴 수 있게 graph.py가 등록하는 타입
STATE_TYPES: Final = (("worker.fix.fix_pure", "MechanicalFix"),)


def out_of_scope(entries: Iterable[StatusEntry], allowed: Iterable[str]) -> list[str]:
    """기계적 수정의 허용 범위 밖 변경 ("XY 경로" 목록). MECHANICAL_POLICY로 판정한다."""
    return [v.label for v in judge_scope(entries, allowed, MECHANICAL_POLICY).violations]


def same_test_result(before: PytestRun, after: PytestRun) -> bool:
    """고친 뒤 테스트가 고치기 전과 같은 상태(통과 또는 테스트 없음)인지. 개수도 같아야 한다."""
    if before.status != after.status or not after.allows_mechanical_fix:
        return False
    if before.summary is None or after.summary is None:
        return before.summary is after.summary or after.status == "no_tests"
    # 불안정한 테스트는 재실행에서 통과한 수까지 센다
    return (before.passed_count, before.summary.total) == (after.passed_count, after.summary.total)


def code_fix_tests_ok(before: PytestRun, after: PytestRun) -> bool:
    """모델 수정 뒤 테스트: 모두 통과하고(불안정한 테스트 없이) 수정 전과 같은 수가 돌았다."""
    if not after.allows_code_fix or before.summary is None or after.summary is None:
        return False
    return (after.passed_count, after.summary.total) == (before.passed_count, before.summary.total)
