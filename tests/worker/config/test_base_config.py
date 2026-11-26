"""app/worker/config/base_config.py: 설정은 PR head가 아니라 base 커밋에서 읽는다. 로컬 git으로
확인한다."""

import subprocess
from pathlib import Path

import pytest

from app.shared import config
from app.worker import update
from app.worker.config import base_config
from app.worker.config.access_pure import AccessSettings
from app.worker.git import repo


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
    repo.record_git_dir(path)  # 파이프라인의 worktree처럼 저장소 경로를 밖에 적는다
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
    ruff = config.ruff.config
    assert ruff is not None and ruff.name == "pyproject.toml"
    assert ruff.text == '[tool.ruff.lint]\nselect = ["F"]\n'
    assert config.problems == ()
    # 설정은 worktree에 꺼내지 않는다. 샌드박스의 ruff에는 검사할 폴더 밖의 사본으로 넘긴다
    assert not (repo_dir / ".pipeline-out").exists()


def test_head_only_config_is_ignored(repo_dir: Path) -> None:
    base = _commit(repo_dir, {"app.py": "x = 1\n"})
    _commit(
        repo_dir, {"ruff.toml": "select = []\n", ".pipeline.yaml": "sandbox:\n  memory_gb: 4\n"}
    )
    config = base_config.load(repo_dir, base, max_memory_gb=4)
    assert config.ruff.config is None  # ruff 설정이 없으면 도구 설정의 규칙도 없다
    assert config.memory_gb == 1


def test_ruff_toml_takes_precedence_over_pyproject(repo_dir: Path) -> None:
    base = _commit(repo_dir, {"ruff.toml": "line-length = 90\n", "pyproject.toml": "[tool.ruff]\n"})
    config = base_config.load(repo_dir, base, max_memory_gb=4)
    assert config.ruff.config is not None and config.ruff.config.name == "ruff.toml"


def test_pyproject_without_ruff_section_is_not_a_ruff_setting(repo_dir: Path) -> None:
    base = _commit(repo_dir, {"pyproject.toml": "[project]\nname = 'x'\n"})
    assert base_config.load(repo_dir, base, max_memory_gb=4).ruff.config is None


def test_broken_pyproject_is_reported(repo_dir: Path) -> None:
    base = _commit(repo_dir, {"pyproject.toml": "[tool.ruff\n"})
    config = base_config.load(repo_dir, base, max_memory_gb=4)
    assert config.ruff.config is None
    assert "pyproject.toml" in config.problems[0]


def test_settings_that_are_symlinks_are_not_read(repo_dir: Path) -> None:
    """설정 파일이 링크면 링크가 가리키는 글을 설정으로 읽지 않는다 (T24)."""
    (repo_dir / "real.yaml").write_text("sandbox:\n  memory_gb: 3\n")
    (repo_dir / ".pipeline.yaml").symlink_to("real.yaml")
    (repo_dir / "ruff.toml").symlink_to("real.yaml")
    base = _commit(repo_dir, {})
    config = base_config.load(repo_dir, base, max_memory_gb=4)
    assert config.memory_gb == 1 and config.ruff.config is None
    assert sorted(config.problems) == [
        "`.pipeline.yaml`이 일반 파일이 아니라 읽지 않음 (심볼릭 링크, 폴더 등)",
        "`ruff.toml`이 일반 파일이 아니라 읽지 않음 (심볼릭 링크, 폴더 등)",
    ]


@pytest.mark.parametrize(
    ("files", "version", "pinned_by"),
    [
        ({}, "0.17.5", ""),
        ({"requirements-dev.txt": "pytest==8.0\nruff==0.17.4  # 린터\n"}, "0.17.4",
         "requirements-dev.txt"),
        ({"pyproject.toml": '[dependency-groups]\ndev = ["ruff==0.16.10"]\n'}, "0.16.10",
         "pyproject.toml"),
        ({"requirements.txt": "ruff>=0.17\n"}, "0.17.5", ""),  # 정확히 고정한 것만 본다
    ],
    ids=["default", "requirements", "dependency-group", "not-pinned"],
)  # fmt: skip
def test_ruff_version_pinned_by_the_base_comes_first(
    repo_dir: Path, files: dict, version: str, pinned_by: str
) -> None:
    """의존성 파일이 ruff 버전을 고정했으면 그 버전, 아니면 파이프라인의 기본 버전 (4.4, 3-4)."""
    base = _commit(repo_dir, files)
    ruff = base_config.load(repo_dir, base, max_memory_gb=4).ruff
    assert (ruff.version, ruff.pinned_by, ruff.available) == (version, pinned_by, True)


def test_pinned_version_the_pipeline_cannot_verify_is_reported(repo_dir: Path) -> None:
    base = _commit(repo_dir, {"requirements.txt": "ruff==0.1.0\n"})
    config = base_config.load(repo_dir, base, max_memory_gb=4)
    assert config.ruff.version == "0.1.0" and not config.ruff.available
    assert config.problems == (
        "ruff 0.1.0 (requirements.txt가 고정)은 파이프라인이 해시를 고정한 버전이 아니라 받지 "
        "않습니다. ruff를 쓰는 규칙은 검사하지 못합니다",
    )


def test_head_cannot_change_the_pinned_version(repo_dir: Path) -> None:
    base = _commit(repo_dir, {"requirements.txt": "ruff==0.17.4\n"})
    _commit(repo_dir, {"requirements.txt": "ruff==0.1.0\n"})
    assert base_config.load(repo_dir, base, max_memory_gb=4).ruff.version == "0.17.4"


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
    assert base_config.load(repo_dir, base, max_memory_gb=4).ruff.config is None


def test_fix_settings_come_from_base_and_are_clamped(repo_dir: Path) -> None:
    """`fix.*`는 base에서만 읽는다. PR이 승인 없이 고치게 바꾸거나 회차를 늘려도 그 PR에는 쓰이지
    않는다. base의 값이 파이프라인 최댓값을 넘으면 줄이고 "설정:"에 알린다 (10절, T13, T23)."""
    base = _commit(
        repo_dir, {".pipeline.yaml": "fix:\n  require_approval: true\n  max_retries: 9\n"}
    )
    _commit(repo_dir, {".pipeline.yaml": "fix:\n  require_approval: false\n  max_retries: 1\n"})
    config = base_config.load(repo_dir, base, max_memory_gb=4)
    assert config.repo.fix.require_approval is True
    assert config.repo.fix.max_retries == 5
    assert config.problems == ("`fix.max_retries` 9는 파이프라인 최댓값 5로 줄였습니다",)


def test_old_fix_key_is_read_with_a_note(repo_dir: Path) -> None:
    base = _commit(repo_dir, {".pipeline.yaml": "skills:\n  fix:\n    require_approval: true\n"})
    config = base_config.load(repo_dir, base, max_memory_gb=4)
    assert config.repo.fix.require_approval is True
    (note,) = config.problems
    assert note.startswith("`skills.fix`는 `fix`로 바뀌었습니다")


def test_old_skills_enabled_without_lint_disables_the_ruff_rule_set(repo_dir: Path) -> None:
    """v1에서 lint를 끈 저장소는 3단계 뒤에도 ruff 규칙이 켜지지 않는다. 새 키를 알린다 (10절)."""
    base = _commit(repo_dir, {".pipeline.yaml": "skills:\n  enabled: []\n", "ruff.toml": ""})
    config = base_config.load(repo_dir, base, max_memory_gb=4)
    assert config.repo.rules.disabled == ["ruff"]
    (note,) = config.problems
    assert "`rules.disabled: [ruff]`로 바꿔 주세요" in note


def test_load_carries_the_access_of_base(repo_dir: Path) -> None:
    """규칙 문서를 PR의 것으로 읽을지 정하는 믿는 작성자의 기준도 base에서 온다."""
    base = _commit(repo_dir, {".pipeline.yaml": "access:\n  trusted: [OWNER]\n"})
    _commit(repo_dir, {".pipeline.yaml": "access:\n  trusted: [NONE]\n"})
    assert base_config.load(repo_dir, base, max_memory_gb=4).access == AccessSettings(
        frozenset({"OWNER"})
    )


# --- 가드레일의 access: 체크아웃 전에 bare 캐시에서 읽는다 (8.6) ---


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    path = tmp_path / "remote"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    return path


def _load_access(tmp_path: Path, remote: Path, base: str) -> AccessSettings:
    return base_config.load_access(tmp_path / "data", "o/r", base, url=str(remote))


def test_access_is_read_from_the_base_commit_without_a_worktree(
    tmp_path: Path, remote: Path
) -> None:
    base = _commit(remote, {".pipeline.yaml": "access:\n  trusted: [OWNER]\n  bots: ['x[bot]']\n"})
    # PR이 자기 자신을 신뢰 목록에 넣는다
    _git(remote, "checkout", "-q", "-b", "pr")
    _commit(remote, {".pipeline.yaml": "access:\n  trusted: [NONE]\n"})
    _git(remote, "update-ref", "refs/pull/1/head", "HEAD")

    access = _load_access(tmp_path, remote, base)

    assert access == AccessSettings(frozenset({"OWNER"}), frozenset({"x[bot]"}))
    assert not (tmp_path / "data" / "worktrees").exists()  # PR 코드를 꺼내지 않는다
    bare = repo.bare_path(tmp_path / "data", "o/r")
    refs = _git(bare, "for-each-ref", "--format=%(refname)")
    assert "refs/pull" not in refs and "refs/pipeline" not in refs


def test_access_fetches_a_base_commit_made_after_the_clone(tmp_path: Path, remote: Path) -> None:
    first = _commit(remote, {"a.py": "x = 1\n"})
    assert _load_access(tmp_path, remote, first) == AccessSettings()
    later = _commit(remote, {".pipeline.yaml": "access:\n  trusted: [MEMBER]\n"})
    assert _load_access(tmp_path, remote, later).trusted == frozenset({"MEMBER"})


def test_access_in_a_symlinked_settings_file_is_not_read(tmp_path: Path, remote: Path) -> None:
    (remote / "real.yaml").write_text("access:\n  trusted: [NONE]\n")
    (remote / ".pipeline.yaml").symlink_to("real.yaml")
    base = _commit(remote, {})
    assert _load_access(tmp_path, remote, base) == AccessSettings()


def test_access_with_an_unknown_commit_fails(tmp_path: Path, remote: Path) -> None:
    _commit(remote, {"a.py": "x = 1\n"})
    with pytest.raises(repo.GitError):
        _load_access(tmp_path, remote, "f" * 40)
    with pytest.raises(repo.GitError):
        _load_access(tmp_path, remote, "--upload-pack=touch")


def test_access_notes_are_shown_with_the_other_settings(repo_dir: Path) -> None:
    base = _commit(repo_dir, {".pipeline.yaml": "access:\n  bots: [alice]\n"})
    (note,) = base_config.load(repo_dir, base, max_memory_gb=4).problems
    assert "`access.bots`" in note and "alice" not in note


def test_pipeline_default_model_fills_stages_the_base_config_leaves_out(repo_dir: Path) -> None:
    base = _commit(repo_dir, {".pipeline.yaml": "model:\n  judge: haiku\n"})
    config = base_config.load(repo_dir, base, max_memory_gb=4, default_model="opus")
    assert (config.repo.model.judge, config.repo.model.rules) == ("haiku", "opus")
    assert (config.repo.model.light, config.repo.model.heavy) == ("opus", "opus")
    plain = base_config.load(repo_dir, base, max_memory_gb=4)
    assert (plain.repo.model.rules, plain.repo.model.light) == ("sonnet", "sonnet")


def test_comment_path_settings_take_the_pipeline_default_model(
    tmp_path: Path, remote: Path
) -> None:
    base = _commit(remote, {".pipeline.yaml": "model:\n  decide: haiku\n"})
    config = base_config.load_repo(tmp_path / "data", "o/r", base, url=str(remote),
                                   default_model="opus")  # fmt: skip
    assert (config.model.decide, config.model.rules) == ("haiku", "opus")


def test_flow_context_carries_the_pipeline_default_model(env) -> None:
    """서버가 읽은 파이프라인 설정의 `model.default`가 base 설정을 읽는 곳까지 간다."""
    deps = env["deps"]
    assert update.flow_context(deps, 0, None).default_model is None
    deps.settings = deps.settings.model_copy(update={"model": config.ModelConfig(default="opus")})
    assert update.flow_context(deps, 0, None).default_model == "opus"


def test_flow_context_carries_the_machine_rag_setting(env) -> None:
    """`config.yaml`의 `rag.allowed`(`pr-proof rag on|off`)가 검색을 정하는 곳까지 간다."""
    deps = env["deps"]
    assert update.flow_context(deps, 0, None).rag_allowed is None
    deps.settings = deps.settings.model_copy(update={"rag": config.RagConfig(allowed=False)})
    assert update.flow_context(deps, 0, None).rag_allowed is False
