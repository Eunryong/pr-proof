"""프롬프트 글 읽기: 단계 폴더의 파일과 그 폴더의 코드가 읽는 이름이 맞는지, 값 넣기, 이름 확인."""

import re
from pathlib import Path

import pytest

import app.worker
from app.worker.core.prompt_text_pure import PROMPT_FOLDER, fill, load

WORKER = Path(app.worker.__file__).parent
PROMPT_DIRS = sorted(p for p in WORKER.rglob(PROMPT_FOLDER) if p.is_dir())


def _loaded_names(folder: Path) -> set[str]:
    """folder의 모듈이 `load(__file__, "...")`로 읽는 이름."""
    names: set[str] = set()
    for path in folder.glob("*.py"):
        names.update(
            re.findall(r'\bload\(\s*__file__,\s*"([a-z_.]+)"', path.read_text(encoding="utf-8"))
        )
    return names


def test_prompt_folders_are_the_stage_folders_and_env() -> None:
    assert [str(p.relative_to(WORKER)) for p in PROMPT_DIRS] == [
        "env/prompts",
        "feedback/prompts",
        "fix/prompts",
        "rules/prompts",
    ]


@pytest.mark.parametrize("prompts", PROMPT_DIRS, ids=lambda p: p.parent.name)
def test_every_prompt_file_is_read_by_a_module_beside_it_and_every_name_has_a_file(
    prompts: Path,
) -> None:
    files = {p.stem for p in prompts.glob("*.md")} - {"README"}
    assert files == _loaded_names(prompts.parent)


@pytest.mark.parametrize("prompts", PROMPT_DIRS, ids=lambda p: p.parent.name)
def test_readme_lists_every_prompt_file_of_its_folder(prompts: Path) -> None:
    readme = (prompts / "README.md").read_text(encoding="utf-8")
    missing = [p.name for p in prompts.glob("*.md") if p.name != "README.md"
               and f"`{p.name}`" not in readme]  # fmt: skip
    assert missing == []


def test_no_module_reads_a_prompt_from_outside_its_folder() -> None:
    """`load`의 첫 인자는 늘 부르는 모듈의 `__file__`이다."""
    calls = []
    for path in WORKER.rglob("*.py"):
        if path.name == "prompt_text_pure.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "prompt_text_pure import load" in text:
            calls += re.findall(r"\bload\(\s*([^,\s)]+)", text)
    assert calls and set(calls) == {"__file__"}


def _module(tmp_path: Path, text: str) -> str:
    (tmp_path / PROMPT_FOLDER).mkdir()
    (tmp_path / PROMPT_FOLDER / "x.md").write_text(text, encoding="utf-8")
    return str(tmp_path / "stage_pure.py")


def test_text_is_the_file_without_its_last_newline(tmp_path: Path) -> None:
    here = _module(tmp_path, "첫 줄\n둘째 줄\n")
    assert load(here, "x") == "첫 줄\n둘째 줄"


def test_values_fill_the_marked_places_and_must_match_them(tmp_path: Path) -> None:
    here = _module(tmp_path, '$tool 을 {"a": 1} 로 $limit 번\n')
    assert load(here, "x", tool="t", limit=3) == 't 을 {"a": 1} 로 3 번'
    assert load(here, "x") == '$tool 을 {"a": 1} 로 $limit 번'  # 값을 주지 않으면 글 그대로
    with pytest.raises(KeyError):
        load(here, "x", tool="t")
    with pytest.raises(ValueError, match="없는 자리: other"):
        load(here, "x", tool="t", limit=3, other=1)


@pytest.mark.parametrize("name", ["../secrets", "a/b", "A", "a.b.c", ""])
def test_names_that_could_leave_the_folder_are_refused(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="프롬프트 이름이 아니다"):
        load(_module(tmp_path, "x\n"), name)


def test_a_missing_file_fails_when_loaded(tmp_path: Path) -> None:
    """단계 모듈은 import할 때 읽으므로, 파일이 빠진 설치본은 워커가 시작할 때 멈춘다."""
    with pytest.raises(FileNotFoundError):
        load(_module(tmp_path, "x\n"), "judge")


def test_pieces_fill_their_places_and_an_empty_piece_removes_its_line() -> None:
    text = "앞\n{{format}}\n\n- 제약\n{{records}}\n\n끝"
    assert fill(text, format="형식 $A", records="- 기록") == "앞\n형식 $A\n\n- 제약\n- 기록\n\n끝"
    assert fill(text, format="형식", records="") == "앞\n형식\n\n- 제약\n\n끝"


def test_values_fill_places_in_the_middle_of_a_line() -> None:
    assert fill('{{tool}}(code="now")로 {{limit}}번', tool="run_tests", limit=3) == (
        'run_tests(code="now")로 3번'
    )


def test_removed_lines_do_not_leave_doubled_blank_lines() -> None:
    text = "제약\n\n{{feedback}}\n\n{{records}}\n\n## 답"
    assert fill(text, feedback="", records="") == "제약\n\n## 답"
    assert fill(text, feedback="", records="기록") == "제약\n\n기록\n\n## 답"
    assert fill(text, feedback="코멘트", records="") == "제약\n\n코멘트\n\n## 답"


def test_pieces_and_places_must_match() -> None:
    with pytest.raises(ValueError, match="조각이 없는 자리: records"):
        fill("{{format}}\n{{records}}", format="x")
    with pytest.raises(ValueError, match="글에 없는 자리: other"):
        fill("{{format}}", format="x", other="y")
