import fakeredis

from app.worker import preflight


def test_all_checks_pass() -> None:
    checks = preflight.run_checks(fakeredis.FakeStrictRedis(), command_ok=lambda args: True)
    assert preflight.failures(checks) == []


def test_failures_include_hint() -> None:
    def command_ok(args: list[str]) -> bool:
        return args[0] != "docker"

    checks = preflight.run_checks(fakeredis.FakeStrictRedis(), command_ok=command_ok)
    assert preflight.failures(checks) == ["Docker 실패 → Docker Desktop 실행"]
