"""app/worker/report/env_report_pure.py: 환경 처방으로 돌린 저장소의 테스트 줄과 환경 블록."""

from typing import Final

from app.worker.env.plan_pure import BaseRun, StepSummary
from app.worker.env.recipe_pure import EnvRecipe, validate
from app.worker.env.use_pure import EnvView
from app.worker.report.env_report_pure import SUMMARY, base_is_red, expanded, folded
from app.worker.report.report import render_result
from app.worker.testing.test_run_pure import EnvUse, StepRun, TestReport, TestRun

HEAD: Final = "a" * 40
RECIPE_JSON: Final = {
    "system": {
        "image": "ubuntu:24.04",
        "commands": ["apt-get update", "apt-get install -y cmake"],
        "fetches": [{"url": "https://github.com/microsoft/vcpkg", "rev": "b" * 40,
                     "dest": "vcpkg"}],
    },
    "deps": {"inputs": ["vcpkg.json"], "commands": ["vcpkg install --x-manifest-root=src"]},
    "overlays": [{"path": "ports/ort/portfile.cmake", "content": "x",
                  "why": "macOS만 받음 `@team` <b>x</b>"}],
    "build": [{"name": "configure", "command": "cmake --preset debug"},
              {"name": "build", "command": "cmake --build --preset debug"}],
    "test": {"name": "ctest", "command": "ctest --output-junit /work/.pipeline-out/j.xml",
             "report": "junit", "report_path": ".pipeline-out/j.xml"},
}  # fmt: skip
GREEN: Final = BaseRun(
    "passed", (StepSummary("configure", 0, 0.3), StepSummary("build", 0, 3.8)),
    TestReport(passed=17, total=17),
)  # fmt: skip
RED: Final = BaseRun(
    "failed", (StepSummary("build", 0, 3.8), StepSummary("ctest", 8, 0.2)),
    TestReport(passed=16, failed=1, total=17), base_is_red="base에서 `ocr` 테스트가 실패함",
)  # fmt: skip
USE: Final = EnvUse(7, layers=(("system", True), ("deps", False)))


def _recipe() -> EnvRecipe:
    recipe = validate(RECIPE_JSON)
    assert isinstance(recipe, EnvRecipe), recipe
    return recipe


def _view(base: BaseRun = GREEN, use: EnvUse = USE) -> EnvView:
    return EnvView(_recipe(), base, use)


PASSED: Final = TestRun(
    build=(StepRun("configure", 0), StepRun("build", 0)), test=StepRun("ctest", 0),
    verdict="passed", report=TestReport(passed=17, total=17), env=USE,
)  # fmt: skip


def test_environment_block_lists_how_the_repository_was_run() -> None:
    text = render_result(PASSED, HEAD, 3.0, env=_view())
    assert "**테스트:** 통과 (17개)\n" in text
    block = text[text.index(f"<details><summary>{SUMMARY}</summary>") :]
    assert "시작 이미지: `ubuntu:24.04`" in block
    assert (
        "시스템 명령 (root, 네트워크 열림):\n\n```\napt-get update\napt-get install -y cmake\n```"
        in (block)
    )
    assert "- `https://github.com/microsoft/vcpkg` 커밋 `bbbbbbbbbbbb` → `vcpkg`" in block
    assert "의존성 입력 (base 커밋의 파일): `vcpkg.json`" in block
    assert "vcpkg install --x-manifest-root=src" in block
    # 모델이 쓴 why는 코드 스팬 안에만 있다 (멘션과 HTML이 동작하지 않는다)
    assert (
        "저장소의 파일 대신 쓴 것:\n- `ports/ort/portfile.cmake`: ``macOS만 받음 `@team` <b>x</b>``"
        in (block)
    )
    assert (
        "configure: cmake --preset debug\nbuild: cmake --build --preset debug\nctest (테스트):"
        in (block)
    )
    assert "층: 시스템 층 다시 씀 · 의존성 층 새로 구움" in block
    assert "처방을 확인할 때의 base 실행: 통과 (17개 중 통과 17개)" in block
    assert block.rstrip().endswith("</details>\n\n<sub>커밋 `aaaaaaa` · 3초</sub>")
    assert "통과하지 않았습니다" not in text  # base가 초록이면 펼친 안내가 없다


COMPILER: Final = "ocr.cpp:3: error: expected ';'"


def test_build_failure_names_the_step_and_shows_the_compiler_output() -> None:
    run = TestRun(
        build=(StepRun("configure", 0), StepRun("build", 2, output=COMPILER)),
        env=USE,
    )  # fmt: skip
    text = render_result(run, HEAD, 3.0, env=_view())
    assert "**테스트:** 빌드 실패로 테스트를 돌리지 못함 (`build` 단계, 종료 코드 2)\n" in text
    assert "<details><summary>출력</summary>\n\n```\nocr.cpp:3: error: expected ';'\n```" in text


def test_a_red_base_is_said_next_to_the_result() -> None:
    run = TestRun(
        build=(StepRun("build", 0),), test=StepRun("ctest", 8), verdict="failed",
        report=TestReport(passed=16, failed=1, total=17, failed_tests=("ocr",)), env=USE,
    )  # fmt: skip
    assert base_is_red(RED) and not base_is_red(GREEN)
    text = render_result(run, HEAD, 3.0, env=_view(RED))
    head = text[: text.index(f"<details><summary>{SUMMARY}</summary>")]
    assert (
        "처방을 확인할 때 base 커밋도 통과하지 않았습니다 (테스트 실패 (17개 중 통과 16개), "
        "`ctest` 단계가 종료 코드 8로 끝남). 이 PR의 실패가 base에서 온 것일 수 있습니다."
    ) in head
    assert "모델이 적은 base의 상태: ``base에서 `ocr` 테스트가 실패함``" in head


def test_ignored_dependency_inputs_are_said_for_an_untrusted_author() -> None:
    use = EnvUse(7, inputs="base", changed_inputs=("vcpkg.json",))
    note = "PR이 바꾼 `vcpkg.json`는 쓰지 않음 (신뢰된 작성자가 아님)"
    assert note in expanded(_view(use=use))
    assert note in folded(_view(use=use))
    trusted = EnvUse(7, inputs="head", changed_inputs=("vcpkg.json",))
    assert "의존성 입력 (PR의 파일 (신뢰된 작성자)): `vcpkg.json`" in folded(_view(use=trusted))
    assert expanded(_view(use=trusted)) == []


def test_a_python_profile_run_has_no_environment_block() -> None:
    assert expanded(None) == [] and folded(None) == []
    run = TestRun(test=StepRun("pytest", 0), verdict="passed", report=TestReport(passed=1, total=1))
    assert SUMMARY not in render_result(run, HEAD, 3.0)
