"""샌드박스 실행에 쓰는 데이터."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Image:
    """샌드박스 베이스 이미지. .venv 캐시 키에 들어간다."""

    id: str
    python_version: str


@dataclass(frozen=True)
class ExecResult:
    """컨테이너 안에서 실행한 명령 하나의 결과."""

    returncode: int | None  # 취소되면 None
    stdout: str
    stderr: str
    timed_out: bool = False
    cancelled: bool = False
