from pathlib import Path

import pytest

from app import config


def test_load_config(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text("repos: [Eunryong/eval]\n")
    settings = config.load_config(tmp_path)
    assert settings.repos == ["Eunryong/eval"]


def test_missing_config_is_error(tmp_path: Path) -> None:
    with pytest.raises(config.ConfigError, match="설정 파일이 없다"):
        config.load_config(tmp_path)


def test_read_secret(tmp_path: Path) -> None:
    path = tmp_path / "webhook_secret"
    path.write_text("s3cret\n")
    path.chmod(0o600)
    assert config.read_secret("webhook_secret", tmp_path) == "s3cret"


@pytest.mark.parametrize(
    ("mode", "content", "message"),
    [(0o644, "s3cret", "권한이 넓다"), (0o600, "  \n", "비어 있다")],
    ids=["readable-by-others", "empty"],
)
def test_bad_secret_is_rejected(tmp_path: Path, mode: int, content: str, message: str) -> None:
    path = tmp_path / "webhook_secret"
    path.write_text(content)
    path.chmod(mode)
    with pytest.raises(config.ConfigError, match=message):
        config.read_secret("webhook_secret", tmp_path)


def test_missing_secret(tmp_path: Path) -> None:
    with pytest.raises(config.ConfigError, match="없다"):
        config.read_secret("webhook_secret", tmp_path)


def test_sandbox_name_defaults_match_docker_cli() -> None:
    from app.sandbox import docker_cli

    sandbox = config.SandboxConfig()
    assert (sandbox.image, sandbox.container_prefix) == (
        docker_cli.IMAGE,
        docker_cli.CONTAINER_PREFIX,
    )


def test_test_names_differ_from_production() -> None:
    from app.sandbox import docker_cli
    from tests.sandbox_names import TEST_NAMES

    assert TEST_NAMES.image != docker_cli.IMAGE
    assert not TEST_NAMES.container_prefix.startswith(docker_cli.CONTAINER_PREFIX)
