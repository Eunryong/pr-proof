"""ensure_image가 기반 이미지를 프로세스 첫 빌드와 그 뒤 하루에 한 번 새로 받는다 (위협 T25). Docker
없이 docker 명령을 가짜로 바꾼다."""

import subprocess

import pytest

from app.sandbox import docker_cli


class FakeDocker:
    def __init__(self, fail_pull: bool = False) -> None:
        self.builds: list[tuple[str, ...]] = []
        self.fail_pull = fail_pull

    def __call__(self, *args: str, **kw) -> subprocess.CompletedProcess[str]:  # noqa: ANN003
        if args[0] == "build":
            self.builds.append(args)
            if "--pull" in args and self.fail_pull:
                raise docker_cli.SandboxError("docker build 실패 (1): network unreachable")
        return subprocess.CompletedProcess(args, 0, "sha256:abc\n", "")


@pytest.fixture
def docker(monkeypatch) -> FakeDocker:
    fake = FakeDocker()
    monkeypatch.setattr(docker_cli, "run", fake)
    return fake


def test_first_build_pulls_then_once_a_day(docker: FakeDocker) -> None:
    clock = [1000.0]
    pulled: dict[str, float] = {}

    def build() -> docker_cli.Image:
        return docker_cli.ensure_image("t", now=lambda: clock[0], pulled_at=pulled)

    assert build().id == "sha256:abc"
    clock[0] += docker_cli.PULL_EVERY_SECONDS - 1
    build()
    clock[0] += 1  # 정확히 하루가 지남
    build()
    build()
    assert ["--pull" in b for b in docker.builds] == [True, False, True, False]
    assert docker_cli.PULL_EVERY_SECONDS == 24 * 60 * 60


def test_each_tag_is_pulled_on_its_own(docker: FakeDocker) -> None:
    pulled: dict[str, float] = {}
    docker_cli.ensure_image("a", now=lambda: 5.0, pulled_at=pulled)
    docker_cli.ensure_image("b", now=lambda: 5.0, pulled_at=pulled)
    assert ["--pull" in b for b in docker.builds] == [True, True]


def test_failed_pull_builds_from_the_local_base_and_waits_a_day(monkeypatch) -> None:
    fake = FakeDocker(fail_pull=True)
    monkeypatch.setattr(docker_cli, "run", fake)
    pulled: dict[str, float] = {}
    assert docker_cli.ensure_image("t", now=lambda: 0.0, pulled_at=pulled).id == "sha256:abc"
    docker_cli.ensure_image("t", now=lambda: 10.0, pulled_at=pulled)
    assert [("--pull" in b) for b in fake.builds] == [True, False, False]
