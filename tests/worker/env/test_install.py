"""app/worker/env/install.py 단위 테스트 (Docker 없이): 의존성 입력 파일 읽기."""

import os
from pathlib import Path

from app.worker.env.install import MAX_INPUT_BYTES, BakeFailed, read_inputs


def test_reads_regular_files_inside_the_repository(tmp_path: Path) -> None:
    (tmp_path / "ports" / "x").mkdir(parents=True)
    (tmp_path / "vcpkg.json").write_bytes(b"{}")
    (tmp_path / "ports" / "x" / "portfile.cmake").write_bytes(b"# x")
    got = read_inputs(tmp_path, ("vcpkg.json", "ports/x/portfile.cmake"))
    assert got == {"vcpkg.json": b"{}", "ports/x/portfile.cmake": b"# x"}


def _rejected(tmp_path: Path, rel: str) -> None:
    got = read_inputs(tmp_path, (rel,))
    assert isinstance(got, BakeFailed), got
    assert (got.layer, got.step) == ("deps", "deps.inputs") and got.output_tail.startswith(rel)


def test_missing_file_is_a_recipe_error(tmp_path: Path) -> None:
    _rejected(tmp_path, "vcpkg.json")


def test_symlinked_file_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_text("secret")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "vcpkg.json").symlink_to(outside)
    _rejected(repo, "vcpkg.json")


def test_symlinked_folder_on_the_way_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f").write_text("secret")
    repo = tmp_path / "repo"
    repo.mkdir()
    os.symlink(outside, repo / "ports")
    _rejected(repo, "ports/f")


def test_folder_and_oversized_file_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "dir").mkdir()
    _rejected(tmp_path, "dir")
    (tmp_path / "big").write_bytes(b"x" * (MAX_INPUT_BYTES + 1))
    _rejected(tmp_path, "big")
