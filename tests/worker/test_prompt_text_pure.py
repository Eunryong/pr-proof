"""프롬프트 글 읽기: 폴더의 파일과 코드가 읽는 이름이 맞는지, 값 넣기, 이름 확인."""

import re
from pathlib import Path

import pytest

from app.worker import prompt_text_pure
from app.worker.prompt_text_pure import PROMPT_DIR, load

APP = Path(prompt_text_pure.__file__).parent.parent


def _loaded_names() -> set[str]:
    """app의 코드가 `load("...")`로 읽는 이름."""
    names: set[str] = set()
    for path in APP.rglob("*.py"):
        names.update(re.findall(r'\bload\(\s*"([a-z_.]+)"', path.read_text(encoding="utf-8")))
    return names


def test_every_prompt_file_is_read_and_every_name_has_a_file() -> None:
    files = {p.stem for p in PROMPT_DIR.glob("*.md")} - {"README"}
    assert files == _loaded_names()


def test_readme_lists_every_prompt_file() -> None:
    readme = (PROMPT_DIR / "README.md").read_text(encoding="utf-8")
    missing = [p.name for p in PROMPT_DIR.glob("*.md") if p.name != "README.md"
               and f"`{p.name}`" not in readme]  # fmt: skip
    assert missing == []


def test_text_is_the_file_without_its_last_newline() -> None:
    raw = (PROMPT_DIR / "judge.md").read_text(encoding="utf-8")
    assert raw.endswith("\n") and load("judge") == raw[:-1]


def test_values_fill_the_marked_places_and_must_match_them(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(prompt_text_pure, "PROMPT_DIR", tmp_path)
    (tmp_path / "x.md").write_text('$tool 을 {"a": 1} 로 $limit 번\n', encoding="utf-8")
    assert load("x", tool="t", limit=3) == 't 을 {"a": 1} 로 3 번'
    assert load("x") == '$tool 을 {"a": 1} 로 $limit 번'  # 값을 주지 않으면 글 그대로
    with pytest.raises(KeyError):
        load("x", tool="t")
    with pytest.raises(ValueError, match="없는 자리: other"):
        load("x", tool="t", limit=3, other=1)


@pytest.mark.parametrize("name", ["../secrets", "a/b", "A", "a.b.c", ""])
def test_names_that_could_leave_the_folder_are_refused(name: str) -> None:
    with pytest.raises(ValueError, match="프롬프트 이름이 아니다"):
        load(name)


def test_a_missing_file_fails_when_loaded(monkeypatch, tmp_path: Path) -> None:
    """단계 모듈은 import할 때 읽으므로, 파일이 빠진 설치본은 워커가 시작할 때 멈춘다."""
    monkeypatch.setattr(prompt_text_pure, "PROMPT_DIR", tmp_path)
    with pytest.raises(FileNotFoundError):
        load("judge")
