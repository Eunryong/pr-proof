from pathlib import Path

import pytest

from shared import config


def test_load_config(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text("repos: [Eunryong/eval]\n")
    settings = config.load_config(tmp_path)
    assert settings.repos == ["Eunryong/eval"]


def test_old_slack_settings_are_ignored(tmp_path: Path) -> None:
    """Slack 운영 알림을 지웠다 (저장소 주인, 2026-11-20). 옛 설정의 키와 남은 URL 파일은 오류
    없이 무시하고 읽지 않는다."""
    (tmp_path / "config.yaml").write_text(
        "repos: [o/r]\nnotify:\n  slack:\n    ops: true\n    webhook_url: https://x\n"
    )
    leftover = tmp_path / "slack_webhook_url"
    leftover.write_text("https://hooks.example/abc\n")
    leftover.chmod(0o644)  # 권한이 넓어도 읽지 않으므로 오류가 아니다
    settings = config.load_config(tmp_path)
    assert settings.repos == ["o/r"] and not hasattr(settings, "notify")


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
    from worker.sandbox import docker_cli

    sandbox = config.SandboxConfig()
    assert (sandbox.image, sandbox.container_prefix) == (
        docker_cli.IMAGE,
        docker_cli.CONTAINER_PREFIX,
    )


def test_test_names_differ_from_production() -> None:
    from tests.sandbox_names import TEST_NAMES
    from worker.sandbox import docker_cli

    assert TEST_NAMES.image != docker_cli.IMAGE
    assert not TEST_NAMES.container_prefix.startswith(docker_cli.CONTAINER_PREFIX)


def test_call_maxima_default_and_override(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text("repos: [o/r]\n")
    assert config.load_config(tmp_path).model.calls.max_repo_daily == 2000
    (tmp_path / "config.yaml").write_text("repos: [o/r]\nmodel:\n  calls:\n    max_pr_max: 7\n")
    calls = config.load_config(tmp_path).model.calls
    assert (calls.max_pr_max, calls.max_ruleset_max) == (7, 300)


def test_rag_embed_model_must_be_a_pinned_model() -> None:
    from pydantic import ValidationError

    from shared.embed_models_pure import E5_SMALL

    assert config.PipelineConfig(repos=[]).rag.embed_model == E5_SMALL
    with pytest.raises(ValidationError):
        config.PipelineConfig(repos=[], rag={"embed_model": "someone/unpinned"})
