"""app/worker/env/install_pure.py 단위 테스트: 실패 나누기, 프록시 기록 보이기, digest 고르기."""

import pytest

from app.worker.env.install_pure import (
    MAX_REFUSED_LINES,
    NO_PROXY_HINT,
    bake_failure_kind,
    contacted,
    egress_note,
    is_docker_failure,
    network_down,
    pick_digest,
)
from app.worker.sandbox.egress_pure import Event


@pytest.mark.parametrize(
    "output",
    [
        "fatal: unable to access 'https://github.com/x.git/': Could not resolve host: github.com",
        "Err:1 http://deb.debian.org/debian bookworm InRelease\n  Temporary failure resolving "
        "'deb.debian.org'",
        "curl: (28) Failed to connect to example.com port 443: Connection timed out",
        "dial tcp: lookup registry-1.docker.io: i/o timeout",
    ],
)
def test_network_markers_are_environment_failures(output: str) -> None:
    assert bake_failure_kind(output) == "network"


@pytest.mark.parametrize(
    "output",
    [
        "E: Unable to locate package no-such-package",
        "sh: 1: vcpkg: not found",
        "error: building opencv4:arm64-linux failed with: BUILD_FAILED",
        "fatal: repository 'https://github.com/owner/missing.git/' not found",
    ],
)
def test_other_failures_are_the_recipes(output: str) -> None:
    assert bake_failure_kind(output) == "recipe"


@pytest.mark.parametrize(
    ("exit_code", "stderr", "docker"),
    [
        (125, "", True),
        (126, 'OCI runtime exec failed: exec: "timeout": executable file not found', True),
        (1, "Error response from daemon: container abc is not running", True),
        (127, "sh: 1: cmake: not found", False),
        (126, "sh: 1: ./build.sh: Permission denied", False),
        (2, "make: *** [all] Error 2", False),
        (None, "", False),
    ],
)
def test_docker_failures_are_told_from_command_failures(
    exit_code: int | None, stderr: str, docker: bool
) -> None:
    assert is_docker_failure(exit_code, stderr) is docker


@pytest.mark.parametrize(
    ("reference", "digests", "expected"),
    [
        ("debian:bookworm-slim", ["debian@sha256:aa"], "debian@sha256:aa"),
        (
            "docker.io/library/debian:12",
            ["other@sha256:bb", "debian@sha256:aa"],
            "debian@sha256:aa",
        ),
        ("ghcr.io/o/i:1", ["ghcr.io/o/i@sha256:cc"], "ghcr.io/o/i@sha256:cc"),
        ("localhost:5000/a:1", ["localhost:5000/a@sha256:dd"], "localhost:5000/a@sha256:dd"),
        ("debian", ["mirror/debian@sha256:ee"], "mirror/debian@sha256:ee"),
        ("debian", [], None),
    ],
)
def test_pick_digest_prefers_the_same_repository(
    reference: str, digests: list[str], expected: str | None
) -> None:
    assert pick_digest(reference, digests) == expected


def _event(verdict: str, host: str = "h.example", port: int = 443, detail: str = "") -> Event:
    return Event(verdict, "CONNECT", host, port, detail)  # type: ignore[arg-type]


@pytest.mark.parametrize("verdict", ["dns_failed", "unreachable"])
def test_proxy_that_cannot_reach_the_network_is_an_environment_failure(verdict: str) -> None:
    events = [_event("allowed"), _event("denied"), _event(verdict, "github.com")]
    assert network_down(events) == events[2]


@pytest.mark.parametrize("verdict", ["allowed", "denied", "unresolved", "refused", "bad_request"])
def test_other_proxy_verdicts_are_not_environment_failures(verdict: str) -> None:
    assert network_down([_event(verdict)]) is None


def test_refused_requests_are_shown_once_each_after_the_output() -> None:
    events = [
        _event("allowed", "github.com"),
        _event("denied", "host.docker.internal", 6379, "192.168.65.254: 공용 주소가 아님"),
        _event("denied", "host.docker.internal", 6379, "192.168.65.254: 공용 주소가 아님"),
        _event("unresolved", "githb.com", 443, "[Errno -2] Name or service not known"),
        Event("bad_request", "", "", 0, "Transfer-Encoding이 있는 요청 본문은 받지 않음"),
    ]
    assert egress_note(events, "curl: (56) CONNECT tunnel failed, response 403") == (
        "\n(프록시가 거절한 요청)"
        "\n- 공용 주소가 아니라 거절: CONNECT host.docker.internal:6379 "
        "(192.168.65.254: 공용 주소가 아님)"
        "\n- 이름이 없음: CONNECT githb.com:443 ([Errno -2] Name or service not known)"
        "\n- 프록시가 받지 않는 요청: (Transfer-Encoding이 있는 요청 본문은 받지 않음)"
    )


def test_many_refused_requests_are_cut_with_a_count() -> None:
    events = [_event("denied", f"10.0.0.{i}") for i in range(MAX_REFUSED_LINES + 3)]
    note = egress_note(events, "")
    assert note.count("\n- 공용 주소가") == MAX_REFUSED_LINES and note.endswith("\n- 그 밖에 3건")


def test_tool_that_skipped_the_proxy_gets_a_hint_and_nothing_else_does() -> None:
    output = "fatal: unable to access 'https://github.com/x/': Could not resolve host: github.com"
    assert egress_note([], output) == "\n" + NO_PROXY_HINT
    assert egress_note([_event("allowed")], "make: *** [all] Error 2") == ""


def test_contacted_hosts_are_the_allowed_targets_in_first_seen_order() -> None:
    events = [_event("allowed", "b.example"), _event("denied", "10.0.0.1"),
              _event("allowed", "a.example", 80), _event("allowed", "b.example")]  # fmt: skip
    assert contacted(events) == ("b.example:443", "a.example:80")
