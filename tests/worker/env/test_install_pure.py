"""app/worker/env/install_pure.py 단위 테스트: 설치 실패 나누기와 이미지 digest 고르기."""

import pytest

from app.worker.env.install_pure import bake_failure_kind, is_docker_failure, pick_digest


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
