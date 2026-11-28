"""층 굽기의 실패 나누기와 이미지 digest 고르기.

처방의 명령이 0이 아닌 값으로 끝나면 처방의 잘못이다(`BakeFailed`, 다음 단계에서 모델이 고친다).
docker 자체가 실패했거나 네트워크가 닿지 않은 것은 처방과 무관한 환경 오류다(`SandboxError`, 작업을
다시 돌린다).
"""

from collections.abc import Sequence
from typing import Final, Literal

# 이름 풀기·연결 실패. apt, curl, git, pip, docker pull이 내는 글이다
NETWORK_MARKERS: Final = (
    "Could not resolve host",
    "Temporary failure in name resolution",
    "Temporary failure resolving",
    "Name or service not known",
    "Connection timed out",
    "connection timed out",
    "Network is unreachable",
    "Could not connect to",
    "Failed to connect to",
    "TLS handshake timeout",
    "i/o timeout",
)
# docker exec가 명령을 띄우지도 못했거나 데몬에 닿지 않음
DOCKER_ERROR_MARKERS: Final = (
    "OCI runtime exec failed",
    "Error response from daemon",
    "Cannot connect to the Docker daemon",
)
DOCKER_ERROR_EXIT: Final = 125  # docker 자신의 오류

FailureKind = Literal["network", "recipe"]


def bake_failure_kind(output: str) -> FailureKind:
    """0이 아닌 값으로 끝난 설치 명령의 출력으로 실패를 나눈다."""
    return "network" if any(m in output for m in NETWORK_MARKERS) else "recipe"


def is_docker_failure(exit_code: int | None, stderr: str) -> bool:
    """docker exec 자체가 실패했는지. 명령이 없을 때의 126·127은 sh가 낸 것이라 보지 않는다."""
    return exit_code == DOCKER_ERROR_EXIT or any(m in stderr for m in DOCKER_ERROR_MARKERS)


def pick_digest(reference: str, repo_digests: Sequence[str]) -> str | None:
    """`docker image inspect`의 RepoDigests에서 참조의 저장소에 맞는 `이름@sha256:…`을 고른다."""
    repo = _repository(reference)
    for entry in repo_digests:
        if _repository(entry.partition("@")[0]) == repo:
            return entry
    return repo_digests[0] if repo_digests else None


def _repository(reference: str) -> str:
    name = reference.partition("@")[0]
    head, _, last = name.rpartition("/")
    name = f"{head}/{last.partition(':')[0]}" if head else last.partition(":")[0]
    return name.removeprefix("docker.io/").removeprefix("library/")


def output_tail(stdout: str, stderr: str, max_chars: int) -> str:
    return (stdout + stderr)[-max_chars:]
