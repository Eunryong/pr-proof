"""층 굽기의 실패 나누기와 이미지 digest 고르기.

처방의 명령이 0이 아닌 값으로 끝나면 처방의 잘못이다(`BakeFailed`, 다음 단계에서 모델이 고친다).
docker 자체가 실패했거나 네트워크가 닿지 않은 것은 처방과 무관한 환경 오류다(`SandboxError`, 작업을
다시 돌린다).

굽는 컨테이너의 네트워크 실패는 출력의 글이 아니라 프록시의 판정으로 나눈다. 굽는 컨테이너에는
프록시 말고 나갈 길이 없어서, 프록시를 쓰지 않는 도구는 네트워크가 멀쩡해도 "Could not resolve
host"를 낸다. 프록시가 이름 서버나 공용 주소에 닿지 못한 것(`NETWORK_VERDICTS`)만 환경 오류다.
없는 이름, 사설 주소, 연결 거절은 처방의 잘못이고 그 호스트를 출력 뒤에 붙인다.
"""

from collections.abc import Mapping, Sequence
from typing import Final, Literal

from app.worker.sandbox.egress_pure import Event, Verdict

# 이름 풀기·연결 실패. apt, curl, git, pip, docker pull이 내는 글이다. 시작 이미지 받기(호스트의
# docker pull)는 이것으로 나눈다. 굽는 명령의 출력에 있으면 프록시를 쓰지 않은 도구로 본다
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
# 프록시가 이름 서버나 공용 주소에 닿지 못함. 네트워크 자체의 문제라 처방이 고칠 수 없다
NETWORK_VERDICTS: Final[frozenset[Verdict]] = frozenset({"dns_failed", "unreachable"})
_REFUSED: Final[Mapping[Verdict, str]] = {
    "denied": "공용 주소가 아니라 거절",
    "unresolved": "이름이 없음",
    "refused": "서버가 연결을 거절",
    "bad_request": "프록시가 받지 않는 요청",
}
MAX_REFUSED_LINES: Final = 20
NO_PROXY_HINT: Final = (
    "(굽는 컨테이너는 HTTP_PROXY·HTTPS_PROXY의 프록시로만 밖에 닿는다. 프록시를 쓰지 않는 도구는 "
    "이름을 풀지 못하고 연결하지 못한다)"
)


def bake_failure_kind(output: str) -> FailureKind:
    """0이 아닌 값으로 끝난 docker pull의 출력으로 실패를 나눈다."""
    return "network" if any(m in output for m in NETWORK_MARKERS) else "recipe"


def network_down(events: Sequence[Event]) -> Event | None:
    """실패한 단계에서 프록시가 네트워크에 닿지 못한 요청. 있으면 환경 오류다."""
    return next((e for e in events if e.verdict in NETWORK_VERDICTS), None)


def egress_note(events: Sequence[Event], output: str) -> str:
    """실패한 단계의 출력 뒤에 붙일 글: 프록시가 거절한 요청과, 프록시를 쓰지 않은 도구의 표지."""
    refused = list(dict.fromkeys(_refused_line(e) for e in events if e.verdict in _REFUSED))
    lines = []
    if refused:
        more = len(refused) - MAX_REFUSED_LINES
        lines += ["(프록시가 거절한 요청)", *refused[:MAX_REFUSED_LINES]]
        lines += [f"- 그 밖에 {more}건"] if more > 0 else []
    if any(m in output for m in NETWORK_MARKERS):
        lines.append(NO_PROXY_HINT)
    return "".join(f"\n{line}" for line in lines)


def _refused_line(event: Event) -> str:
    parts = (event.method, event.host and event.target(), event.detail and f"({event.detail})")
    return f"- {_REFUSED[event.verdict]}: " + " ".join(part for part in parts if part)


def contacted(events: Sequence[Event]) -> tuple[str, ...]:
    """프록시가 연결해 준 곳(`호스트:포트`), 중복 없이 처음 순서대로."""
    return tuple(dict.fromkeys(e.target() for e in events if e.verdict == "allowed"))


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
