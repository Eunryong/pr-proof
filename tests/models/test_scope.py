"""models/scope.py: porcelain 출력 읽기와 허용 범위 판정."""

from dataclasses import replace

import pytest

from app.models.scope import (
    AI_POLICY,
    MECHANICAL_POLICY,
    PIPELINE_OUTPUTS,
    StatusEntry,
    is_test_file,
    judge_scope,
    parse_ignored,
    parse_porcelain,
)
from app.sandbox.container import OUT_DIR, WORKDIR
from app.sandbox.venv import VENV_IN_CONTAINER


def test_pipeline_outputs_match_sandbox_paths() -> None:
    venv = VENV_IN_CONTAINER.removeprefix(WORKDIR + "/")
    assert set(PIPELINE_OUTPUTS) == {f"{OUT_DIR}/", f"{venv}/"}


def test_parse_copy_and_unmerged_and_ignored() -> None:
    out = "C  b.py\0a.py\0UU c.py\0!! build/x\0T  d.py\0"
    assert parse_porcelain(out) == [
        StatusEntry("C ", "b.py", "a.py"),
        StatusEntry("UU", "c.py"),
        StatusEntry("!!", "build/x"),
        StatusEntry("T ", "d.py"),
    ]


def test_parse_path_with_newline() -> None:
    assert parse_porcelain("?? a\nb.py\0") == [StatusEntry("??", "a\nb.py")]


def test_parse_ignored_keeps_only_ignored_and_drops_outputs() -> None:
    out = "?? new.py\0!! build/x\0!! .pipeline-out/r.json\0!! .venv/bin/py\0"
    assert parse_ignored(out) == {"build/x"}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/data/input.json", True),
        ("pkg/tests/helpers.py", True),
        ("test/x.py", True),
        ("src/test_app.py", True),
        ("src/app_test.py", True),
        ("conftest.py", True),
        ("build/conftest.py", True),
        ("src/app.py", False),
        ("src/testing.py", False),
        ("src/test_data.json", False),  # .py가 아닌 test_ 파일
        ("src/contest.py", False),
        ("tests", False),  # 폴더 이름과 같은 파일
        ("src/latest/app.py", False),
    ],
)
def test_is_test_file(path: str, expected: bool) -> None:
    assert is_test_file(path) is expected


def _reasons(entries, allowed=("a.py",), policy=AI_POLICY, **kw) -> list[tuple[str, str, str]]:
    result = judge_scope(entries, allowed, policy, **kw)
    return [(v.code, v.path, v.reason) for v in result.violations]


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        (StatusEntry(" M", "a.py"), []),
        (StatusEntry("MM", "a.py"), []),
        (StatusEntry("A ", "a.py"), []),
        (StatusEntry("??", "a.py"), []),
        (StatusEntry(" D", "a.py"), []),
        (StatusEntry(" M", "b.py"), [("M", "b.py", "허용 목록 밖")]),
        (StatusEntry("UU", "a.py"), [("UU", "a.py", "병합 충돌 상태")]),
        (StatusEntry(" T", "a.py"), [("T", "a.py", "파일 종류 변경")]),
        (StatusEntry("R ", "a.py", "old.py"), [("R", "old.py", "허용 목록 밖")]),
        (StatusEntry("C ", "new.py", "a.py"), [("C", "new.py", "허용 목록 밖")]),
        (StatusEntry("??", "nested/"), [("??", "nested/", "허용 목록 밖")]),  # 안쪽 git 리포
        (StatusEntry("R ", ".pipeline-out/x", "a.py"), [("R", ".pipeline-out/x", "허용 목록 밖")]),
        (StatusEntry("??", ".pipeline-out/x"), []),
    ],
    ids=lambda v: repr(v) if isinstance(v, StatusEntry) else "",
)
def test_ai_policy_statuses(entry: StatusEntry, expected) -> None:
    assert _reasons([entry]) == expected


def test_rename_reports_both_paths_outside() -> None:
    assert _reasons([StatusEntry("R ", "x/new.py", "x/old.py")]) == [
        ("R", "x/new.py", "허용 목록 밖"),
        ("R", "x/old.py", "허용 목록 밖"),
    ]


def test_test_file_in_allowed_is_still_rejected() -> None:
    entries = [StatusEntry(" M", "tests/test_a.py")]
    assert _reasons(entries, allowed=("tests/test_a.py",)) == [
        ("M", "tests/test_a.py", "테스트 파일 변경")
    ]


def test_test_files_allowed_hook() -> None:
    policy = replace(AI_POLICY, test_files_allowed=frozenset({"tests/test_a.py"}))
    entries = [StatusEntry(" M", "tests/test_a.py")]
    assert _reasons(entries, allowed=("tests/test_a.py",), policy=policy) == []
    # 예외 목록에 있어도 허용 목록 밖이면 버린다
    assert _reasons(entries, allowed=(), policy=policy) == [
        ("M", "tests/test_a.py", "허용 목록 밖")
    ]


def test_symlink_and_new_ignored() -> None:
    entries = [StatusEntry("??", "a.py")]
    got = _reasons(
        entries, symlinks=frozenset({"a.py"}), new_ignored={"z/conftest.py", ".pipeline-out/r"}
    )
    assert got == [("??", "a.py", "심볼릭 링크"), ("!!", "z/conftest.py", "새로 생긴 무시된 파일")]


def test_every_violation_is_listed_and_detail_has_reasons() -> None:
    entries = [StatusEntry(" M", "b.py"), StatusEntry(" M", ".github/ci.yml")]
    result = judge_scope(entries, ["a.py"], AI_POLICY)
    assert not result.ok
    assert result.detail == "M b.py (허용 목록 밖), M .github/ci.yml (`.github/` 변경)"
    assert judge_scope([], ["a.py"], AI_POLICY).ok


@pytest.mark.parametrize(
    ("entry", "labels"),
    [
        (StatusEntry(" M", "tests/test_a.py"), []),  # lint는 테스트 파일의 위반도 고친다
        (StatusEntry("??", "tests/test_a.py"), ["?? tests/test_a.py"]),
        (StatusEntry(" T", "tests/test_a.py"), ["T tests/test_a.py"]),
        (StatusEntry(" D", "tests/test_a.py"), ["D tests/test_a.py"]),
    ],
)
def test_lint_policy_modify_only(entry: StatusEntry, labels: list[str]) -> None:
    result = judge_scope([entry], ["tests/test_a.py"], MECHANICAL_POLICY)
    assert [v.label for v in result.violations] == labels
