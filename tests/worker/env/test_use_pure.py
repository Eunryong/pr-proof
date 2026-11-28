"""app/worker/env/use_pure.py: 처방이 없는 까닭의 글, 의존성 입력과 작성자 신뢰, 굽지 못한 실행."""

import pytest

from app.worker.env.install_pure import BakeFailed
from app.worker.env.use_pure import (
    InputsChoice,
    bake_failed_run,
    choose_inputs,
    ignored_inputs_note,
    unplanned_reason,
)
from app.worker.testing.test_run_pure import EnvUse

INPUTS = ("vcpkg.json", "ports/onnxruntime/portfile.cmake")


def test_nothing_to_run_quotes_the_model_reason_in_a_code_span() -> None:
    text = unplanned_reason("nothing_to_run", "문서만 있음 @team `x`", rounds=1, last_round="")
    assert text.startswith("모델이 base 커밋을 읽고 돌릴 빌드·테스트가 없다고 답함: ")
    assert text.endswith("`` 문서만 있음 @team `x` ``")  # 백틱이 든 글도 코드 스팬을 닫지 못한다
    assert "Python뿐" not in text


def test_unverified_names_the_rounds_and_the_last_failing_step() -> None:
    text = unplanned_reason(
        "unverified",
        "출력 끝",
        rounds=3,
        last_round="층 system 구움, base 실행 build_failed (build)",
    )
    assert text == (
        "모델이 쓴 환경 처방이 base 커밋에서 3회 안에 통과하지 못함 "
        "(마지막 회차: `층 system 구움, base 실행 build_failed (build)`)"
    )
    assert "출력 끝" not in text  # 출력은 줄에 넣지 않는다


def test_unverified_without_a_round_says_only_the_count() -> None:
    assert unplanned_reason("unverified", "", rounds=0, last_round="") == (
        "모델이 쓴 환경 처방이 base 커밋에서 0회 안에 통과하지 못함"
    )


def test_cap_and_model_failure_say_what_stopped_the_plan() -> None:
    assert unplanned_reason("cap", "저장소당 하루 10회", rounds=0, last_round="") == (
        "환경 처방을 정하는 모델 호출이 상한에 걸림 (저장소당 하루 10회)"
    )
    failed = unplanned_reason("model_failed", "timeout: 600초", rounds=0, last_round="")
    assert failed == "환경 처방을 정하는 모델 호출이 실패함: `timeout: 600초`"


@pytest.mark.parametrize(
    ("changed", "trusted", "expected"),
    [
        ((), True, InputsChoice("base", ())),
        ((), False, InputsChoice("base", ())),
        (("src/a.cpp",), True, InputsChoice("base", ())),
        (("vcpkg.json", "src/a.cpp"), True, InputsChoice("head", ("vcpkg.json",))),
        (("vcpkg.json",), False, InputsChoice("base", ("vcpkg.json",))),
        (
            ("ports/onnxruntime/portfile.cmake", "vcpkg.json"),
            False,
            InputsChoice("base", INPUTS),
        ),
    ],
    ids=["none", "none-untrusted", "other-file", "trusted", "untrusted", "untrusted-both"],
)
def test_head_inputs_are_baked_only_for_a_trusted_author(
    changed: tuple[str, ...], trusted: bool, expected: InputsChoice
) -> None:
    assert choose_inputs(changed, INPUTS, trusted=trusted) == expected


def test_ignored_inputs_are_named_only_when_the_author_was_not_trusted() -> None:
    ignored = EnvUse(1, inputs="base", changed_inputs=("vcpkg.json",))
    assert (
        ignored_inputs_note(ignored) == "PR이 바꾼 `vcpkg.json`는 쓰지 않음 (신뢰된 작성자가 아님)"
    )
    assert ignored_inputs_note(EnvUse(1, inputs="head", changed_inputs=("vcpkg.json",))) is None
    assert ignored_inputs_note(EnvUse(1)) is None


def test_a_failed_head_inputs_bake_is_unrunnable_with_the_step_and_output() -> None:
    use = EnvUse(1, inputs="head", changed_inputs=("vcpkg.json",))
    run = bake_failed_run(BakeFailed("deps", "deps.commands[1]", 1, "error: no port"), use)
    assert run.status == "unrunnable"
    assert run.unrunnable_reason == (
        "PR이 바꾼 의존성 입력으로 환경을 준비하지 못함 (`deps.commands[1]`, 종료 코드 1)"
    )
    assert run.output == "error: no port" and run.env == use


def test_a_failed_base_bake_does_not_blame_the_pr() -> None:
    run = bake_failed_run(BakeFailed("system", "system.commands[0]", None, "", timed_out=True),
                          EnvUse(1))  # fmt: skip
    assert run.status == "unrunnable"
    assert run.unrunnable_reason == (
        "base에서 확인한 환경을 다시 준비하지 못함 (`system.commands[0]`, 시간 상한을 넘음)"
    )
    unreadable = bake_failed_run(BakeFailed("deps", "deps.inputs", None, "없음"), EnvUse(1))
    assert "입력을 읽지 못함" in unreadable.unrunnable_reason
