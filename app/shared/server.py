"""이 서버의 id. 데이터 폴더(`settings.data_dir`)의 파일 하나이고, 데이터 폴더가 서버의 정체다.

처음 필요할 때 짧은 무작위 값으로 만들어 두고 그 뒤로 읽기만 한다. 설정 파일은 건드리지 않는다.
두 프로세스가 동시에 처음 불러도 값이 하나가 되도록, 다른 이름으로 쓴 뒤 없을 때만 그 이름으로
링크한다.
"""

import os
import re
import secrets
from pathlib import Path
from typing import Final

from app.shared import version
from app.shared.other_server_pure import Server

ID_FILE: Final = "server_id"
_ID: Final = re.compile(r"[0-9a-f]{8,32}")


def read_id(data_dir: Path) -> str | None:
    """저장한 id. 없거나 꼴이 맞지 않으면 None이고 아무것도 쓰지 않는다."""
    try:
        text = (data_dir / ID_FILE).read_text().strip()
    except OSError:
        return None
    return text if _ID.fullmatch(text) else None


def load_id(data_dir: Path) -> str:
    """저장한 id. 없으면 만들어 적는다. 꼴이 맞지 않는 파일은 새 값으로 바꾼다."""
    found = read_id(data_dir)
    if found is not None:
        return found
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / ID_FILE
    tmp = data_dir / f".{ID_FILE}.{os.getpid()}.{secrets.token_hex(4)}"
    tmp.write_text(secrets.token_hex(4) + "\n")
    try:
        try:
            os.link(tmp, path)
        except FileExistsError:
            # 링크로 만든 파일은 처음부터 값이 다 들어 있다. 다른 프로세스가 먼저 만들었으면 그
            # 값을 쓰고, 꼴이 맞지 않는 파일일 때만 새 값으로 바꾼다
            if read_id(data_dir) is None:
                os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    found = read_id(data_dir)
    if found is None:
        raise OSError(f"서버 id를 적지 못함: {path}")
    return found


def current(data_dir: Path) -> Server:
    """이 서버의 id와 판."""
    return Server(id=load_id(data_dir), version=version.package())
