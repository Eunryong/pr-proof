import pytest

from app.models.repo_config import MIN_MEMORY_GB, memory_limit, parse_repo_config


def test_missing_file_uses_defaults() -> None:
    config, problem = parse_repo_config(None)
    assert config.sandbox.memory_gb == 1 and problem is None


def test_empty_file_uses_defaults() -> None:
    assert parse_repo_config("")[1] is None


def test_reads_memory_and_ignores_other_sections() -> None:
    text = "sandbox:\n  memory_gb: 2\nskills:\n  enabled: [lint]\nmodel:\n  light: haiku\n"
    config, problem = parse_repo_config(text)
    assert config.sandbox.memory_gb == 2 and problem is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("sandbox: [", "YAML 형식 오류"),
        ("- a\n- b\n", "최상위가 매핑이 아님"),
        ("sandbox:\n  memory_gb: lots\n", "`sandbox.memory_gb` 값 오류"),
        (f"sandbox:\n  memory_gb: {MIN_MEMORY_GB / 2}\n", "`sandbox.memory_gb` 값 오류"),
    ],
    ids=["yaml", "not-mapping", "type", "too-small"],
)
def test_invalid_config_falls_back_with_reason(text: str, expected: str) -> None:
    config, problem = parse_repo_config(text)
    assert config.sandbox.memory_gb == 1  # 작업을 멈추지 않고 기본값으로 검사한다
    assert problem is not None and expected in problem


def test_minimum_memory_is_accepted() -> None:
    config, problem = parse_repo_config(f"sandbox:\n  memory_gb: {MIN_MEMORY_GB}\n")
    assert config.sandbox.memory_gb == MIN_MEMORY_GB and problem is None


@pytest.mark.parametrize(("requested", "expected"), [(2, 2), (4, 4), (8, 4)])
def test_memory_limit_is_capped(requested: float, expected: float) -> None:
    assert memory_limit(requested, 4) == expected
