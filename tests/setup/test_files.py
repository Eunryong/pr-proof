"""app/setup/files.py: 안내에 쓸 명령 앞부분."""

import pytest

from app.setup import files


@pytest.mark.parametrize("given", ["pr-proof", "npx pr-proof", "npx ."])
def test_prefix_is_what_the_entry_passed(given: str) -> None:
    """입구(bin/pr-proof.js)가 자기가 불린 방식(전역 설치, npx, clone한 폴더의 npx .)을 넘긴다."""
    assert files.command_prefix({files.COMMAND_ENV: given}) == given
    assert files.command("start", {files.COMMAND_ENV: given}) == f"{given} start"


def test_without_the_entry_the_folder_decides(monkeypatch) -> None:
    """`python -m app.setup.cli`로 직접 부르면 값이 없다. clone한 폴더면 `npx .`, 아니면
    `pr-proof`."""
    assert files.command_prefix({}) == "npx ."  # 테스트는 clone한 폴더에서 돈다
    assert files.command_prefix({files.COMMAND_ENV: "  "}) == "npx ."
    monkeypatch.setattr(files, "in_clone", lambda: False)
    assert files.command_prefix({}) == "pr-proof"


def test_files_on_disk_always_say_pr_proof(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(files.COMMAND_ENV, "npx .")
    files.write_port(8800, tmp_path)
    text = (tmp_path / files.RUN_ENV_FILE).read_text()
    assert text.startswith("# pr-proof start가") and "API_PORT=8800" in text
