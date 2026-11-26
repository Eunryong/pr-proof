"""app/worker/fix/scope_pure.py: porcelain 출력 읽기와 허용 범위 판정."""

from dataclasses import replace
from typing import Final

import pytest

from app.worker.fix.scope_pure import (
    AI_POLICY,
    MECHANICAL_POLICY,
    PIPELINE_OUTPUTS,
    StatusEntry,
    generated_test_dir,
    is_generated_test,
    is_test_file,
    judge_scope,
    parse_ignored,
    parse_porcelain,
)
from app.worker.lang.python import VENV_IN_CONTAINER
from app.worker.sandbox.container import OUT_DIR, WORKDIR


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
        (StatusEntry(" M", "b.py"), []),  # 위반을 고치며 같이 바꾼 다른 코드 파일
        (StatusEntry(" M", "b.toml"), [("M", "b.toml", "허용 목록 밖")]),
        (StatusEntry(" D", "b.py"), [("D", "b.py", "허용 목록 밖")]),
        (StatusEntry("??", "b.py"), [("??", "b.py", "허용 목록 밖")]),
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
    entries = [StatusEntry(" M", "b.cfg"), StatusEntry(" M", ".github/ci.yml")]
    result = judge_scope(entries, ["a.py"], AI_POLICY)
    assert not result.ok
    assert result.detail == "M b.cfg (허용 목록 밖), M .github/ci.yml (`.github/` 변경)"
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


GEN: Final = replace(AI_POLICY, generated_test_dir="tests")


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        (StatusEntry("??", "tests/test_pipeline_orders.py"), []),
        (StatusEntry(" A", "tests/test_pipeline_orders.py"), []),
        (StatusEntry(" M", "tests/test_pipeline_orders.py"),
         [("M", "tests/test_pipeline_orders.py", "테스트 파일 변경")]),
        (StatusEntry(" D", "tests/test_pipeline_orders.py"),
         [("D", "tests/test_pipeline_orders.py", "테스트 파일 변경")]),
        (StatusEntry(" M", "tests/test_a.py"), [("M", "tests/test_a.py", "테스트 파일 변경")]),
        (StatusEntry(" D", "tests/test_a.py"), [("D", "tests/test_a.py", "테스트 파일 변경")]),
        (StatusEntry("??", "tests/conftest.py"), [("??", "tests/conftest.py", "테스트 파일 변경")]),
        (StatusEntry("??", "tests/sub/test_pipeline_x.py"),
         [("??", "tests/sub/test_pipeline_x.py", "테스트 파일 변경")]),
        (StatusEntry("??", "tests/test_pipeline_X.py"),
         [("??", "tests/test_pipeline_X.py", "테스트 파일 변경")]),
        (StatusEntry("??", "test/test_pipeline_x.py"),
         [("??", "test/test_pipeline_x.py", "테스트 파일 변경")]),
        (StatusEntry("??", "pytest.ini"), [("??", "pytest.ini", "허용 목록 밖")]),
        (StatusEntry("R ", "tests/test_pipeline_x.py", "tests/test_a.py"),
         [("R", "tests/test_pipeline_x.py", "테스트 파일 변경"),
          ("R", "tests/test_a.py", "테스트 파일 변경")]),
    ],
    ids=lambda v: repr(v) if isinstance(v, StatusEntry) else "",
)  # fmt: skip
def test_only_new_generated_test_files_are_allowed(entry: StatusEntry, expected) -> None:
    """만든 테스트는 정한 자리의 정한 이름으로 새로 만든 파일뿐이다. 있던 테스트, conftest, 설정은
    밖이다."""
    assert _reasons([entry], policy=GEN) == expected


def test_without_a_test_dir_no_test_file_can_be_created() -> None:
    entry = StatusEntry("??", "tests/test_pipeline_x.py")
    assert _reasons([entry]) == [("??", "tests/test_pipeline_x.py", "테스트 파일 변경")]


def test_a_generated_test_that_is_a_symlink_is_still_rejected() -> None:
    entry = StatusEntry("??", "tests/test_pipeline_x.py")
    got = _reasons([entry], policy=GEN, symlinks=frozenset({entry.path}))
    assert got == [("??", entry.path, "심볼릭 링크")]


@pytest.mark.parametrize(
    ("dirs", "expected"),
    [(["tests", "test"], "tests"), (["test", "src"], "test"), (["src"], "tests"), ([], "tests")],
)
def test_generated_test_dir(dirs, expected) -> None:
    assert generated_test_dir(dirs) == expected


def test_generated_test_name_rule() -> None:
    assert is_generated_test("tests/test_pipeline_a1_b.py", "tests")
    assert not is_generated_test("tests/test_pipeline_.py", "tests")
    assert not is_generated_test("tests/test_pipeline_" + "a" * 41 + ".py", "tests")
    assert not is_generated_test("tests/test_pipeline_a.pyc", "tests")
    assert not is_generated_test("tests/../test_pipeline_a.py", "tests")


def test_other_code_files_are_accepted_only_as_edits_of_the_same_kind() -> None:
    """모델 수정은 위반을 고치며 같이 바꾼 다른 코드 파일의 수정을 받는다. 테스트 파일, 설정 파일,
    목록 밖의 새 파일·이름 변경은 여전히 밖이다."""
    allowed = ["orders/models/cart.py"]
    assert judge_scope([StatusEntry(" M", "orders/api/cart_handlers.py")], allowed, AI_POLICY).ok
    outside = [
        StatusEntry(" M", "tests/test_cart_handlers.py"),
        StatusEntry(" M", "pyproject.toml"),
        StatusEntry(" M", "Makefile"),
        StatusEntry("??", "orders/api/new_handlers.py"),
        StatusEntry("R ", "orders/api/moved.py", "orders/api/cart_handlers.py"),
    ]
    for entry in outside:
        assert not judge_scope([entry], allowed, AI_POLICY).ok, entry
    # 기계적 수정은 위반이 있는 파일만이다
    assert not judge_scope(
        [StatusEntry(" M", "orders/api/cart_handlers.py")], allowed, MECHANICAL_POLICY
    ).ok
