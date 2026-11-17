"""worker/base_config.py: 설정은 PR head가 아니라 base 커밋에서 읽는다. 로컬 git으로 확인한다."""

import subprocess
from pathlib import Path

import pytest

from app.worker import base_config


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip


def _commit(path: Path, files: dict[str, str]) -> str:
    for name, text in files.items():
        (path / name).write_text(text)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "--allow-empty", "-m", "c")
    return _git(path, "rev-parse", "HEAD")


@pytest.fixture
def repo_dir(tmp_path: Path) -> Path:
    path = tmp_path / "wt"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    return path


def test_reads_base_not_head(repo_dir: Path) -> None:
    base = _commit(
        repo_dir,
        {
            ".pipeline.yaml": "sandbox:\n  memory_gb: 2\n",
            "pyproject.toml": '[tool.ruff.lint]\nselect = ["F"]\n',
        },
    )
    # PR이 기준을 바꾼다: 한도를 올리고 ruff 규칙을 끈다
    _commit(
        repo_dir,
        {
            ".pipeline.yaml": "sandbox:\n  memory_gb: 4\n",
            "pyproject.toml": "[tool.ruff.lint]\nselect = []\n",
        },
    )
    config = base_config.load(repo_dir, base, max_memory_gb=4)
    assert config.memory_gb == 2
    assert config.ruff_source == "pyproject.toml"
    written = repo_dir / config.ruff_config
    assert written.read_text() == '[tool.ruff.lint]\nselect = ["F"]\n'
    assert config.problems == ()


def test_head_only_config_is_ignored(repo_dir: Path) -> None:
    base = _commit(repo_dir, {"app.py": "x = 1\n"})
    _commit(
        repo_dir, {"ruff.toml": "select = []\n", ".pipeline.yaml": "sandbox:\n  memory_gb: 4\n"}
    )
    config = base_config.load(repo_dir, base, max_memory_gb=4)
    assert config.ruff_config is None
    assert config.ruff_source == base_config.RUFF_DEFAULT_SOURCE
    assert config.memory_gb == 1


def test_ruff_toml_takes_precedence_over_pyproject(repo_dir: Path) -> None:
    base = _commit(repo_dir, {"ruff.toml": "line-length = 90\n", "pyproject.toml": "[tool.ruff]\n"})
    config = base_config.load(repo_dir, base, max_memory_gb=4)
    assert config.ruff_source == "ruff.toml"
    assert config.ruff_config == f"{base_config.BASE_DIR}/ruff.toml"


def test_pyproject_without_ruff_section_uses_defaults(repo_dir: Path) -> None:
    base = _commit(repo_dir, {"pyproject.toml": "[project]\nname = 'x'\n"})
    assert base_config.load(repo_dir, base, max_memory_gb=4).ruff_config is None


def test_broken_pyproject_is_reported(repo_dir: Path) -> None:
    base = _commit(repo_dir, {"pyproject.toml": "[tool.ruff\n"})
    config = base_config.load(repo_dir, base, max_memory_gb=4)
    assert config.ruff_config is None
    assert "pyproject.toml" in config.problems[0]


def test_memory_is_capped_by_pipeline(repo_dir: Path) -> None:
    base = _commit(repo_dir, {".pipeline.yaml": "sandbox:\n  memory_gb: 16\n"})
    config = base_config.load(repo_dir, base, max_memory_gb=4)
    assert config.memory_gb == 4
    assert config.repo.sandbox.memory_gb == 16


def test_broken_pipeline_yaml_falls_back(repo_dir: Path) -> None:
    base = _commit(repo_dir, {".pipeline.yaml": "sandbox: [\n"})
    config = base_config.load(repo_dir, base, max_memory_gb=4)
    assert config.memory_gb == 1
    assert ".pipeline.yaml" in config.problems[0]


def test_nested_files_with_same_name_are_not_read(repo_dir: Path) -> None:
    (repo_dir / "sub").mkdir()
    base = _commit(repo_dir, {"sub/ruff.toml": "line-length = 50\n"})
    assert base_config.load(repo_dir, base, max_memory_gb=4).ruff_config is None
