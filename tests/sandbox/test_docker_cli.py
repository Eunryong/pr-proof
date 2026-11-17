"""app/sandbox/docker_cli.py 단위 테스트."""

from app.sandbox import docker_cli


def test_minimal_env_drops_secrets(monkeypatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = docker_cli.minimal_env()
    assert env["PATH"] == "/usr/bin"
    assert "GH_TOKEN" not in env and "ANTHROPIC_API_KEY" not in env
