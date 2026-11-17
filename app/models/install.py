"""의존성 설치 실패를 PR 문제와 환경 오류로 나누는 규칙 (docs/DESIGN.md F8)."""

from typing import Final, Literal

InstallFailureKind = Literal["resolution", "network", "unknown"]
InstallOutcome = Literal["unrunnable", "environment"]

# 원인을 판단할 수 없는 실패가 같은 의존성 해시로 이만큼 연속되면 PR 문제로 본다
UNKNOWN_FAILURE_LIMIT: Final = 2


def install_failure_outcome(kind: InstallFailureKind, consecutive_failures: int) -> InstallOutcome:
    """설치 실패의 처리.

    - resolution: PR의 의존성 파일 문제(없는 패키지, 버전 충돌, 잘못된 형식). 1회에 실행 불가
    - network: 환경 오류. 큐에 되돌린다
    - unknown: 판단할 수 없으면 consecutive_failures(이번 실패 포함)가 한도에 닿을 때만 실행 불가
    """
    if kind == "resolution":
        return "unrunnable"
    if kind == "network":
        return "environment"
    return "unrunnable" if consecutive_failures >= UNKNOWN_FAILURE_LIMIT else "environment"
