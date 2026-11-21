"""app/sandbox/docker_cli.py 단위 테스트."""

from worker.sandbox import docker_cli


def test_minimal_env_drops_secrets(monkeypatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = docker_cli.minimal_env()
    assert env["PATH"] == "/usr/bin"
    assert "GH_TOKEN" not in env and "ANTHROPIC_API_KEY" not in env


def test_limits_drop_all_capabilities_and_privilege_escalation() -> None:
    args = docker_cli.limits(1.5)
    assert args[:2] == ("--memory", "1536m")
    pairs = set(zip(args, args[1:], strict=False))
    assert ("--cap-drop", "ALL") in pairs
    assert ("--security-opt", "no-new-privileges") in pairs
    assert ("--pids-limit", "256") in pairs
