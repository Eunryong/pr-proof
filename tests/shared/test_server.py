"""서버 id(데이터 폴더의 파일)와 패키지의 판."""

import json
from pathlib import Path

from app.shared import server, version


def test_id_is_made_once_and_then_only_read(tmp_path: Path) -> None:
    data = tmp_path / "data"
    assert server.read_id(data) is None
    assert not data.exists()  # 읽기만으로는 아무것도 쓰지 않는다
    first = server.load_id(data)
    assert len(first) == 8 and int(first, 16) >= 0
    assert (data / server.ID_FILE).read_text().strip() == first
    assert server.load_id(data) == first and server.read_id(data) == first
    assert [p.name for p in data.iterdir()] == [server.ID_FILE]  # 임시 파일이 남지 않는다


def test_another_data_dir_is_another_server(tmp_path: Path) -> None:
    assert server.load_id(tmp_path / "a") != server.load_id(tmp_path / "b")


def test_broken_id_file_is_replaced(tmp_path: Path) -> None:
    (tmp_path / server.ID_FILE).write_text("이상한 값\n")
    assert server.read_id(tmp_path) is None
    made = server.load_id(tmp_path)
    assert server.read_id(tmp_path) == made


def test_package_version_is_the_package_json_version() -> None:
    data = json.loads((version.REPO_ROOT / "package.json").read_text())
    assert version.package() == data["version"]


def test_current_server(tmp_path: Path) -> None:
    me = server.current(tmp_path)
    assert (me.id, me.version) == (server.read_id(tmp_path), version.package())
